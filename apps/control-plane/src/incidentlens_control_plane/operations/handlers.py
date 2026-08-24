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

from incidentlens_control_plane.changes.manager import ChangeManager
from incidentlens_control_plane.operations.types import Operation
from incidentlens_control_plane.project_registry.store import (
    ProjectNotFound,
    ProjectRegistryStore,
)
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.targets.store import TargetNotFound, TargetStore


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
    "build_rollback_handler",
    "build_target_test_handler",
]
