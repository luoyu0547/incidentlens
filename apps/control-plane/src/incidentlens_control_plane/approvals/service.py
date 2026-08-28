"""Async approval service that emits durable runtime events."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from incidentlens_control_plane.approvals.store import (
    ApprovalAlreadyDecided,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalStore,
    ApprovalUnavailable,
    intent_sha256,
)
from incidentlens_control_plane.approvals.types import (
    ApprovalDownstreamStatus,
    ApprovalRecord,
    ApprovalStatus,
)
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import JsonValue, RuntimeEvent, RuntimeEventType


class ApprovalMismatch(Exception):
    """Raised when consumed intent does not match the approved canonical hash."""


# Re-export for test convenience
ApprovalUnavailable = ApprovalUnavailable  # noqa: F811
ApprovalExpired = ApprovalExpired  # noqa: F811
ApprovalAlreadyDecided = ApprovalAlreadyDecided  # noqa: F811


def _redact_summary(intent: Mapping[str, JsonValue]) -> str:
    """Produce a human-readable, redacted summary from an intent mapping."""
    kind = intent.get("kind", "unknown")
    target = intent.get("target_id", "unknown")
    container = intent.get("container")
    if container:
        return f"{kind} {container} on {target}"
    return f"{kind} on {target}"


class ApprovalService:
    """Manages exact, single-use approval lifecycle with event emission."""

    def __init__(
        self,
        approvals: ApprovalStore,
        events: RuntimeEventStore,
        broker: RuntimeEventBroker,
    ) -> None:
        self._approvals = approvals
        self._events = events
        self._broker = broker

    def list(
        self,
        status: ApprovalStatus | None = None,
        *,
        target_id: str | None = None,
        session_id: str | None = None,
        investigation_id: str | None = None,
        allowed_target_ids: frozenset[str] | None = None,
    ) -> tuple[ApprovalRecord, ...]:
        """List approval records, optionally filtered by status."""
        return self._approvals.list(
            status,
            target_id=target_id,
            session_id=session_id,
            investigation_id=investigation_id,
            allowed_target_ids=allowed_target_ids,
        )

    def list_page(
        self,
        *,
        status: ApprovalStatus | None,
        target_id: str | None,
        session_id: str | None,
        investigation_id: str | None,
        allowed_target_ids: frozenset[str] | None,
        limit: int,
        after_created_at: datetime | None,
        after_approval_id: str | None,
    ) -> tuple[tuple[ApprovalRecord, ...], bool]:
        return self._approvals.list_page(
            status=status,
            target_id=target_id,
            session_id=session_id,
            investigation_id=investigation_id,
            allowed_target_ids=allowed_target_ids,
            limit=limit,
            after_created_at=after_created_at,
            after_approval_id=after_approval_id,
        )

    def get(self, approval_id: str) -> ApprovalRecord | None:
        """Return the persisted approval record by id, or ``None``.

        Decision handlers must read from here rather than trusting a caller
        supplied record, so a forged or expired ``ApprovalRecord`` can never
        authorize a mutation.
        """
        return self._approvals.get(approval_id)

    async def request(
        self,
        intent: dict[str, JsonValue],
        *,
        now: datetime | None = None,
        project_id: str | None = None,
        target_id: str | None = None,
        service: str | None = None,
        session_id: str | None = None,
        investigation_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
        changeset_id: str | None = None,
        proposal_id: str | None = None,
        risk: str = "approval_required",
        preview: Mapping[str, JsonValue] | None = None,
    ) -> ApprovalRecord:
        """Create a new approval request and emit APPROVAL_REQUESTED event."""
        now = now or datetime.now(UTC)
        now = now.astimezone(UTC)
        approval_id = f"apr-{uuid.uuid4().hex[:12]}"
        sha = intent_sha256(intent)
        summary = _redact_summary(intent)

        record = self._approvals.create_request(
            approval_id=approval_id,
            intent_sha256=sha,
            intent=intent,
            intent_summary=summary,
            now=now,
            project_id=project_id,
            target_id=target_id,
            service=service,
            session_id=session_id,
            investigation_id=investigation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            changeset_id=changeset_id,
            proposal_id=proposal_id,
            risk=risk,
            preview=preview,
        )

        event = RuntimeEvent(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            sequence=0,
            event_type=RuntimeEventType.APPROVAL_REQUESTED,
            occurred_at=now,
            payload={
                "approval_id": record.approval_id,
                "kind": intent.get("kind", "unknown"),
                "target_id": intent.get("target_id", "unknown"),
                "status": record.status.value,
            },
        )
        stored_event = self._events.append(event)
        await self._broker.publish(stored_event)

        return record

    def mark_downstream(
        self,
        approval_id: str,
        status: ApprovalDownstreamStatus,
        *,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        """Persist downstream processing state independent of the decision."""
        return self._approvals.mark_downstream(
            approval_id,
            status,
            error_code=error_code,
            now=now,
        )

    async def approve(
        self,
        approval_id: str,
        *,
        now: datetime | None = None,
        actor: str | None = None,
        reason: str | None = None,
        route_key: str | None = None,
        idempotency_key: str | None = None,
        request_sha256: str | None = None,
    ) -> ApprovalRecord:
        """Approve a pending request and emit APPROVAL_APPROVED event."""
        now = now or datetime.now(UTC)
        now = now.astimezone(UTC)

        record = self._approvals.approve(
            approval_id,
            now,
            actor=actor,
            reason=reason,
            route_key=route_key,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )

        event = RuntimeEvent(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            sequence=0,
            event_type=RuntimeEventType.APPROVAL_APPROVED,
            occurred_at=now,
            payload={
                "approval_id": record.approval_id,
                "kind": record.intent.get("kind", "unknown"),
                "target_id": record.intent.get("target_id", "unknown"),
                "status": record.status.value,
            },
        )
        stored_event = self._events.append(event)
        await self._broker.publish(stored_event)

        return record

    async def reject(
        self,
        approval_id: str,
        *,
        now: datetime | None = None,
        actor: str | None = None,
        reason: str | None = None,
        route_key: str | None = None,
        idempotency_key: str | None = None,
        request_sha256: str | None = None,
    ) -> ApprovalRecord:
        """Reject a pending request and emit APPROVAL_REJECTED event."""
        now = now or datetime.now(UTC)
        now = now.astimezone(UTC)

        record = self._approvals.reject(
            approval_id,
            now,
            actor=actor,
            reason=reason,
            route_key=route_key,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )

        event = RuntimeEvent(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            sequence=0,
            event_type=RuntimeEventType.APPROVAL_REJECTED,
            occurred_at=now,
            payload={
                "approval_id": record.approval_id,
                "kind": record.intent.get("kind", "unknown"),
                "target_id": record.intent.get("target_id", "unknown"),
                "status": record.status.value,
            },
        )
        stored_event = self._events.append(event)
        await self._broker.publish(stored_event)

        return record

    async def consume(
        self,
        approval_id: str,
        intent: dict[str, JsonValue],
        *,
        now: datetime | None = None,
        actor: str | None = None,
        reason: str | None = None,
    ) -> ApprovalRecord:
        """Consume an approval after verifying the canonical intent matches.

        Raises ApprovalMismatch if the provided intent does not match the
        original canonical hash. Raises ApprovalUnavailable if the approval
        is expired, rejected, or already consumed.
        """
        now = now or datetime.now(UTC)
        now = now.astimezone(UTC)

        existing = self._approvals.get(approval_id)
        if existing is None:
            raise ApprovalNotFound(f"Approval '{approval_id}' not found")

        if intent_sha256(intent) != existing.intent_sha256:
            raise ApprovalMismatch(
                "Intent hash does not match the approved canonical parameters"
            )

        record = self._approvals.consume(approval_id, now)

        event = RuntimeEvent(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            sequence=0,
            event_type=RuntimeEventType.APPROVAL_CONSUMED,
            occurred_at=now,
            payload={
                "approval_id": record.approval_id,
                "kind": record.intent.get("kind", "unknown"),
                "target_id": record.intent.get("target_id", "unknown"),
                "status": record.status.value,
            },
        )
        stored_event = self._events.append(event)
        await self._broker.publish(stored_event)

        return record
