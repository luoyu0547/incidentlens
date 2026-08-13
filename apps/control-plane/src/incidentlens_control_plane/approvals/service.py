"""Async approval service that emits durable runtime events."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from incidentlens_control_plane.approvals.store import (
    ApprovalNotFound,
    ApprovalStore,
    ApprovalUnavailable,
    intent_sha256,
)
from incidentlens_control_plane.approvals.types import ApprovalRecord, ApprovalStatus
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import JsonValue, RuntimeEvent, RuntimeEventType


class ApprovalMismatch(Exception):
    """Raised when consumed intent does not match the approved canonical hash."""


# Re-export for test convenience
ApprovalUnavailable = ApprovalUnavailable  # noqa: F811


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
        self, status: ApprovalStatus | None = None
    ) -> tuple[ApprovalRecord, ...]:
        """List approval records, optionally filtered by status."""
        return self._approvals.list(status)

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

    async def approve(
        self,
        approval_id: str,
        *,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        """Approve a pending request and emit APPROVAL_APPROVED event."""
        now = now or datetime.now(UTC)
        now = now.astimezone(UTC)

        record = self._approvals.approve(approval_id, now)

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
    ) -> ApprovalRecord:
        """Reject a pending request and emit APPROVAL_REJECTED event."""
        now = now or datetime.now(UTC)
        now = now.astimezone(UTC)

        record = self._approvals.reject(approval_id, now)

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
