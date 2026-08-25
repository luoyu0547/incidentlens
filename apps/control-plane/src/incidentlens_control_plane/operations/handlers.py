"""Durable operation handlers (the worker-side execution contract).

A handler is an awaitable callable keyed by :class:`OperationKind` that receives
the claimed :class:`Operation` -- reading the fields it needs from the
already-redacted ``request_payload`` -- and returns a safe
:class:`OperationResult` summary.  The dispatcher owns claim/heartbeat and the
transition to a terminal status, so a handler never touches the operation store.

Handlers are pure: their service dependencies are bound at registration time in
``build_runtime`` through the ``build_*_handler`` factories, so this module stays
importable without a runtime container.

Recovery never auto-replays a dangerous handler (ROLLBACK): a ROLLBACK that was
RUNNING when the process died is parked UNCERTAIN, and only a queued (never
started) ROLLBACK is dispatched by the worker loop.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from incidentlens_control_plane.agent_sessions.store import AgentSessionStore
from incidentlens_control_plane.agent_sessions.types import AgentSessionStatus
from incidentlens_control_plane.changes.manager import ChangeManager
from incidentlens_control_plane.investigation.service import InvestigationService
from incidentlens_control_plane.investigation.state_machine import (
    INVESTIGATION_STATE_MACHINE,
)
from incidentlens_control_plane.investigation.types import AgentScope
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.operations.types import Operation
from incidentlens_control_plane.project_registry.store import (
    ProjectNotFound,
    ProjectRegistryStore,
)
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.targets.store import TargetNotFound, TargetStore

if TYPE_CHECKING:
    from incidentlens_control_plane.agent_sessions.service import AgentSessionService


class OperationHandlerError(Exception):
    """Raised by a handler for a deterministic failure with a safe message."""


@dataclass(frozen=True)
class OperationResult:
    """The safe outcome of one handler run.

    ``summary`` is a redacted, bounded progress summary.  When ``error_code`` is
    set the dispatcher moves the operation to ``failed`` instead of ``succeeded``.
    """

    summary: str
    error_code: str | None = None
    error_message: str | None = None


OperationHandler = Callable[[Operation], Awaitable[OperationResult]]


def build_agent_message_handler(
    *,
    sessions: AgentSessionStore,
    session_service: AgentSessionService,
    investigations: InvestigationService,
    projects: ProjectRegistryStore,
    target_store: TargetStore,
) -> OperationHandler:
    """Run one accepted message through the existing investigation services."""

    async def handler(operation: Operation) -> OperationResult:
        payload = _payload(operation)
        message_id = payload.get("message_id")
        action = payload.get("action", "message")
        if not isinstance(message_id, str) or not message_id:
            raise OperationHandlerError("agent message payload is malformed")
        session_id = operation.session_id
        if not session_id:
            raise OperationHandlerError("agent message operation has no session")
        session = sessions.get_session(session_id)
        message = sessions.get_message(message_id)
        if message.session_id != session_id:
            raise OperationHandlerError("agent message does not belong to session")
        try:
            binding = target_store.get(session.target_id)
            record = projects.get(binding.project_id)
        except (TargetNotFound, ProjectNotFound) as exc:
            raise OperationHandlerError("session target is unavailable") from exc
        service_name = session.service_id or (
            record.services[0].compose_service if record.services else "host"
        )
        investigation_id = session.investigation_id
        if investigation_id is None:
            if action != "message":
                raise OperationHandlerError("resume requires an investigation")
        else:
            try:
                existing_investigation = investigations.get_investigation(investigation_id)
            except Exception as exc:  # noqa: BLE001 - domain lookup becomes safe handler failure
                raise OperationHandlerError("session investigation is unavailable") from exc
            if INVESTIGATION_STATE_MACHINE.is_terminal(existing_investigation.status):
                if action != "message":
                    raise OperationHandlerError("resume requires a non-terminal investigation")
                investigation_id = None
        if investigation_id is None:
            investigation = investigations.create_investigation(
                project_id=binding.project_id,
                target_id=binding.registry_target_id,
                service=service_name,
                symptom=message.content_redacted,
            )
            investigation_id = investigation.investigation_id
            session = sessions.bind_investigation(
                session_id,
                investigation_id,
                now=operation.updated_at,
                status=AgentSessionStatus.ACTIVE,
            )
            sessions.bind_message(
                message_id,
                investigation_id=investigation_id,
                agent_run_id=None,
                transcript_sequence=None,
            )
        if investigation_id is None:
            raise OperationHandlerError("session investigation binding failed")
        scope = AgentScope(
            project_id=binding.project_id,
            target_id=binding.registry_target_id,
            scope=LogScope.HOST,
            service_name=None,
        )
        run = await investigations.start(investigation_id, scope)
        session_service.bind_investigation(
            session_id,
            investigation_id,
            now=operation.updated_at,
            status=AgentSessionStatus.ACTIVE,
        )
        session_service.project_run(
            session_id,
            run,
            user_message_id=message_id,
            now=operation.updated_at,
        )
        return OperationResult(summary="agent message execution completed")

    return handler


def build_rollback_handler(changes: ChangeManager) -> OperationHandler:
    """Bind a ROLLBACK handler to the ChangeManager it restores through."""

    async def handler(operation: Operation) -> OperationResult:
        payload = _payload(operation)
        changeset_id = payload.get("changeset_id")
        if not isinstance(changeset_id, str) or not changeset_id:
            raise OperationHandlerError("rollback payload is missing changeset_id")
        approval_id = payload.get("approval_id")
        if approval_id is not None and not isinstance(approval_id, str):
            raise OperationHandlerError("rollback payload has a malformed approval_id")
        await changes.rollback(changeset_id, approval_id)
        return OperationResult(summary=f"changeset {changeset_id} rolled back")

    return handler


def build_target_test_handler(
    *,
    target_store: TargetStore,
    projects: ProjectRegistryStore,
    sessions: SessionManager,
) -> OperationHandler:
    """Bind a TARGET_TEST handler to the facade/registry/session services.

    The probe resolves the v1 facade target through its binding to the
    authoritative ProjectRegistry record, connects a live host session and
    reports reachability plus the registered services -- a read-only capability
    probe that recovery may safely requeue and re-run.
    """

    async def handler(operation: Operation) -> OperationResult:
        try:
            binding = target_store.get(operation.target_id)
        except TargetNotFound as exc:
            raise OperationHandlerError(
                f"target not found: {operation.target_id}"
            ) from exc
        try:
            record = projects.get(binding.project_id)
        except ProjectNotFound as exc:
            raise OperationHandlerError(
                f"target project no longer exists: {binding.project_id}"
            ) from exc
        target_reg = next(
            (
                target
                for target in record.targets
                if target.target_id == binding.registry_target_id
            ),
            None,
        )
        if target_reg is None:
            raise OperationHandlerError(
                f"target {operation.target_id!r} is no longer registered"
            )
        session = await sessions.connect(target_reg)
        reachable = await session.transport.is_alive()
        services = ", ".join(svc.compose_service for svc in record.services) or "none"
        return OperationResult(
            summary=f"reachable={reachable}; services=[{services}]"
        )

    return handler


def _payload(operation: Operation) -> dict[str, object]:
    """Parse the redacted request payload into a JSON object."""
    if operation.request_payload is None:
        return {}
    try:
        value = json.loads(operation.request_payload)
    except json.JSONDecodeError:
        raise OperationHandlerError("operation payload is not valid JSON") from None
    if not isinstance(value, dict):
        raise OperationHandlerError("operation payload must be a JSON object")
    return value


__all__ = [
    "OperationHandler",
    "OperationHandlerError",
    "OperationResult",
    "build_agent_message_handler",
    "build_rollback_handler",
    "build_target_test_handler",
]
