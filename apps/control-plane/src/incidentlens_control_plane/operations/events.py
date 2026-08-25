"""Durable runtime-event helpers for the Operations domain.

Every publisher method emits through the shared ``/api/events`` store and broker
(no second stream).  Payloads carry ONLY identifiers, the kind/status and a
bounded redacted summary preview — never ``request_payload``, raw error text or
claim metadata.  ``emit`` appends to the durable store synchronously and
delivers to live subscribers without blocking, so it is safe to call from both
async and synchronous orchestration paths.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType
from incidentlens_control_plane.logs.redaction import redact_message
from incidentlens_control_plane.operations.types import Operation, OperationStatus

_JsonValue = str | int | float | bool | None | list[Any] | dict[str, Any]

_STATUS_EVENT_TYPES: dict[OperationStatus, RuntimeEventType] = {
    OperationStatus.QUEUED: RuntimeEventType.OPERATION_QUEUED,
    OperationStatus.RUNNING: RuntimeEventType.OPERATION_RUNNING,
    OperationStatus.CANCEL_REQUESTED: RuntimeEventType.OPERATION_CANCEL_REQUESTED,
    OperationStatus.SUCCEEDED: RuntimeEventType.OPERATION_SUCCEEDED,
    OperationStatus.FAILED: RuntimeEventType.OPERATION_FAILED,
    OperationStatus.CANCELLED: RuntimeEventType.OPERATION_CANCELLED,
    OperationStatus.UNCERTAIN: RuntimeEventType.OPERATION_UNCERTAIN,
}


class OperationEventPublisher:
    """Publishes redacted operation events through the shared stream."""

    def __init__(
        self,
        events: RuntimeEventStore,
        broker: RuntimeEventBroker,
    ) -> None:
        self._events = events
        self._broker = broker

    def emit(
        self,
        event_type: RuntimeEventType,
        *,
        occurred_at: datetime | None = None,
        **payload: _JsonValue,
    ) -> RuntimeEvent:
        """Append one event durably and deliver it to live subscribers."""
        event = RuntimeEvent(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            sequence=0,
            event_type=event_type,
            occurred_at=(occurred_at or datetime.now(UTC)).astimezone(UTC),
            payload=dict(payload),
        )
        stored = self._events.append(event)
        self._publish(stored)
        return stored

    def _publish(self, stored: RuntimeEvent) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. a synchronous unit test): publish inline.
            asyncio.run(self._broker.publish(stored))
        else:
            loop.create_task(self._broker.publish(stored))

    # -- operation lifecycle ---------------------------------------------------

    def operation_status_changed(
        self, operation: Operation, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.emit(
            _STATUS_EVENT_TYPES[operation.status],
            occurred_at=occurred_at,
            operation_id=operation.operation_id,
            kind=operation.kind.value,
            status=operation.status.value,
            target_id=operation.target_id,
            session_id=operation.session_id,
            investigation_id=operation.investigation_id,
            summary_preview=self._preview(operation.progress_summary),
        )

    def operation_queued(
        self, operation: Operation, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.operation_status_changed(operation, occurred_at=occurred_at)

    def operation_running(
        self, operation: Operation, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.operation_status_changed(operation, occurred_at=occurred_at)

    def operation_cancel_requested(
        self, operation: Operation, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.operation_status_changed(operation, occurred_at=occurred_at)

    def operation_succeeded(
        self, operation: Operation, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.operation_status_changed(operation, occurred_at=occurred_at)

    def operation_failed(
        self, operation: Operation, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.operation_status_changed(operation, occurred_at=occurred_at)

    def operation_cancelled(
        self, operation: Operation, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.operation_status_changed(operation, occurred_at=occurred_at)

    def operation_uncertain(
        self, operation: Operation, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.operation_status_changed(operation, occurred_at=occurred_at)

    @staticmethod
    def _preview(value: object | None, width: int = 600) -> str | None:
        if value is None:
            return None
        return redact_message(str(value), max_length=width).message_redacted


__all__ = ["OperationEventPublisher"]
