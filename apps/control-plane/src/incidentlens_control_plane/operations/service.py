"""Boundary service for durable operations.

The service owns the durable operation lifecycle: creation (always ``queued``),
atomic single-worker claims, state-machine-validated transitions, idempotent
cancellation and the redacted/bounded field policy.  Redacted payloads, safe
summaries and bounded errors are computed here — raw text never reaches the
store, and events carry only ids/status/safe summaries.

Request payloads are handled specially: they must be valid JSON, and redaction
walks the JSON tree so keys, quotes, separators and non-string scalars stay
intact while every string value is redacted (bound per value, never to the
2,000-char error-message bound).  A Task 7 worker must be able to parse the
stored payload to execute the operation, so the envelope is never line-rewritten
or structurally corrupted.

Cancellation semantics:

- ``queued -> cancelled`` (terminal)
- ``running -> cancel_requested`` (non-terminal; the worker still owns it)
- ``cancel_requested`` / ``cancelled`` -> stays (idempotent no-op)
- ``succeeded`` / ``failed`` / ``uncertain`` -> raise ``OperationNotCancellable``
"""

from __future__ import annotations

import json
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
#: Payload values are bounded per value at the redaction module's default; the
#: payload envelope itself is never truncated so a worker keeps durable input.
_MAX_PAYLOAD_VALUE_LENGTH = 16 * 1024

#: JSON field keys whose entire string value is secret and replaced outright.
_PASSWORD_FIELD_KEYS = frozenset({"password", "passwd", "pwd", "secret"})
_SECRET_FIELD_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "api-key",
        "access_key",
        "access-token",
        "auth_token",
        "authorization",
        "bearer",
        "client_secret",
        "secret_key",
        "private_key",
        "signature",
        "credential",
        "credentials",
    }
)


class OperationPayloadInvalid(ValueError):
    """Raised when an operation request payload is not valid JSON."""


def _redact_json_value(value: object, *, key_hint: str | None = None) -> object:
    """Redact every string inside a JSON value without breaking the envelope.

    A string under a secret-looking field key is replaced wholesale with a
    stable placeholder; any other string is passed through the deterministic
    log redactor (which bounds it per value).  Keys, quotes, separators and
    non-string scalars are preserved exactly.
    """
    if isinstance(value, dict):
        return {
            key: _redact_json_value(item, key_hint=key)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(item, key_hint=key_hint) for item in value]
    if isinstance(value, str):
        if key_hint is not None and key_hint.lower() in _SECRET_FIELD_KEYS:
            if key_hint.lower() in _PASSWORD_FIELD_KEYS:
                return "[REDACTED_PASSWORD]"
            return "[REDACTED_TOKEN]"
        return redact_message(
            value, max_length=_MAX_PAYLOAD_VALUE_LENGTH
        ).message_redacted
    return value


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
                self._redact_json_payload(request_payload)
                if request_payload is not None
                else None
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

    @staticmethod
    def _redact_json_payload(payload: str) -> str:
        """Validate *payload* as JSON and return its JSON-preserving redaction.

        A non-JSON payload raises :class:`OperationPayloadInvalid` before it can
        reach the store.  The redacted payload is produced by walking the parsed
        JSON tree (see :func:`_redact_json_value`) and re-serializing it, so the
        stored text is always valid JSON that a worker can parse to execute the
        operation.
        """
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise OperationPayloadInvalid(
                "request payload must be valid JSON"
            ) from exc
        redacted = _redact_json_value(value)
        return json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))


__all__ = ["OperationPayloadInvalid", "OperationService"]
