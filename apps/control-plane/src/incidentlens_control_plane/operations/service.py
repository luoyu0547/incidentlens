"""Boundary service for durable operations.

The service owns the durable operation lifecycle: creation (always ``queued``),
atomic single-worker claims, state-machine-validated transitions, idempotent
cancellation and the redacted/bounded field policy.  Redacted payloads, safe
summaries and bounded errors are computed here — raw text never reaches the
store, and events carry only ids/status/safe summaries.

Cancellation semantics:

- ``queued -> cancelled`` (terminal)
- ``running -> cancel_requested`` (non-terminal; the worker still owns it)
- ``cancel_requested`` / ``cancelled`` -> stays (idempotent no-op)
- ``succeeded`` / ``failed`` / ``uncertain`` -> raise ``OperationNotCancellable``
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from incidentlens_control_plane.logs.redaction import redact_message
from incidentlens_control_plane.operations.events import OperationEventPublisher
from incidentlens_control_plane.operations.state_machine import OperationNotCancellable
from incidentlens_control_plane.operations.store import OperationStore
from incidentlens_control_plane.operations.types import (
    Operation,
    OperationKind,
    OperationStatus,
    OperationView,
)

#: Bound stored error text so failure messages never grow unbounded.
_MAX_ERROR_MESSAGE_LENGTH = 2000
_MAX_PROGRESS_SUMMARY_LENGTH = 2000


class OperationService:
    """Boundary service implementing the durable operation lifecycle."""

    def __init__(
        self,
        *,
        store: OperationStore,
        publisher: OperationEventPublisher,
    ) -> None:
        self._store = store
        self._publisher = publisher

    # -- creation ------------------------------------------------------------

    def create_operation(
        self,
        *,
        kind: OperationKind,
        target_id: str,
        created_by: str,
        session_id: str | None = None,
        investigation_id: str | None = None,
        request_payload: str | None = None,
        progress_summary: str | None = None,
        now: datetime | None = None,
    ) -> Operation:
        """Queue a new operation; the payload is redacted before it is stored."""
        now_utc = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        operation = Operation(
            operation_id=f"op_{uuid.uuid4().hex[:24]}",
            kind=kind,
            status=OperationStatus.QUEUED,
            target_id=target_id,
            created_by=created_by,
            session_id=session_id,
            investigation_id=investigation_id,
            request_payload=(
                self._redact(request_payload) if request_payload is not None else None
            ),
            progress_summary=(
                self._redact(
                    progress_summary, max_length=_MAX_PROGRESS_SUMMARY_LENGTH
                )
                if progress_summary is not None
                else None
            ),
            created_at=now_utc,
            updated_at=now_utc,
        )
        stored = self._store.create(operation)
        self._publisher.operation_queued(stored)
        return stored

    # -- read surface ----------------------------------------------------------

    def get_operation(self, operation_id: str) -> Operation:
        """Return one persisted operation, or raise OperationNotFound."""
        return self._store.get(operation_id)

    def list_queued(self, *, limit: int = 100) -> tuple[Operation, ...]:
        """Return claimable operations, oldest first."""
        return self._store.list_queued(limit=limit)

    # -- claim -----------------------------------------------------------------

    def claim(
        self, operation_id: str, *, worker: str, now: datetime | None = None
    ) -> Operation:
        """Atomically claim one queued operation as a running attempt."""
        now_utc = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        claimed = self._store.claim(operation_id, claim_token=worker, now=now_utc)
        self._publisher.operation_running(claimed)
        return claimed

    # -- transition -----------------------------------------------------------

    def transition(
        self,
        operation_id: str,
        target: OperationStatus,
        *,
        progress_summary: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> Operation:
        """Move an operation to ``target`` after state-machine validation.

        Field values are redacted and bounded before they reach the store.
        """
        now_utc = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        operation = self._store.get(operation_id)
        updated = self._store.transition(
            operation,
            target,
            now=now_utc,
            progress_summary=(
                self._redact(progress_summary, max_length=_MAX_PROGRESS_SUMMARY_LENGTH)
                if progress_summary is not None
                else None
            ),
            error_code=error_code,
            error_message=(
                self._redact(error_message, max_length=_MAX_ERROR_MESSAGE_LENGTH)
                if error_message is not None
                else None
            ),
        )
        self._publisher.operation_status_changed(updated)
        return updated

    # -- cancellation -----------------------------------------------------------

    def cancel(
        self, operation_id: str, *, now: datetime | None = None
    ) -> Operation:
        """Cancel an operation idempotently.

        Repeated cancellations succeed and return the current operation (an
        already-``cancelled`` or ``cancel_requested`` operation is a no-op).
        Calling cancel on a terminal ``succeeded`` / ``failed`` / ``uncertain``
        operation raises ``OperationNotCancellable``.
        """
        now_utc = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        operation = self._store.get(operation_id)
        current = operation.status
        if current in (
            OperationStatus.CANCEL_REQUESTED,
            OperationStatus.CANCELLED,
        ):
            return operation
        if current == OperationStatus.QUEUED:
            return self.transition(
                operation_id, OperationStatus.CANCELLED, now=now_utc
            )
        if current == OperationStatus.RUNNING:
            return self.transition(
                operation_id, OperationStatus.CANCEL_REQUESTED, now=now_utc
            )
        raise OperationNotCancellable(
            f"operation {operation_id} is {current.value!r} and cannot be cancelled"
        )

    # -- rendering --------------------------------------------------------------

    def to_view(self, operation: Operation) -> OperationView:
        """Render the client-facing view, omitting private fields."""
        return OperationView(
            operation_id=operation.operation_id,
            kind=operation.kind,
            status=operation.status,
            target_id=operation.target_id,
            session_id=operation.session_id,
            investigation_id=operation.investigation_id,
            progress_summary=operation.progress_summary,
            error_code=operation.error_code,
            error_message=operation.error_message,
            created_at=operation.created_at,
            updated_at=operation.updated_at,
            finished_at=operation.finished_at,
        )

    @staticmethod
    def _redact(value: str, *, max_length: int = _MAX_ERROR_MESSAGE_LENGTH) -> str:
        return redact_message(value, max_length=max_length).message_redacted


__all__ = ["OperationService"]
