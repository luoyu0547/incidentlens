"""Wire and persistence types for durable Operations.

An :class:`Operation` is long-running work addressed at a product target.  The
persisted model carries the full row — including the redacted ``request_payload``
and single-worker claim metadata — while the on-the-wire
:class:`OperationView` deliberately omits ``request_payload``, ``claim_token``
and ``created_by`` so the client contract is exactly the declared shape and
request payloads never echo back to a caller.

All models are frozen and reject unknown fields so the product contract can
never drift from an accidental extra key.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class OperationKind(StrEnum):
    """The stable kind vocabulary for durable operations."""

    AGENT_MESSAGE = "agent_message"
    TARGET_TEST = "target_test"
    INVESTIGATION_START = "investigation_start"
    ROLLBACK = "rollback"
    REPORT_GENERATE = "report_generate"


class OperationStatus(StrEnum):
    """The durable operation lifecycle statuses."""

    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


class Operation(BaseModel):
    """A persisted ``operations`` row.

    ``request_payload`` is the already-redacted JSON text (never raw input) and
    ``error_message`` is redacted and bounded (<= 2000 chars).  ``claim_token`` /
    ``claimed_at`` hold the single-worker claim; both are internal and never
    serialized to clients.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    kind: OperationKind
    status: OperationStatus
    target_id: str
    created_by: str
    session_id: str | None = None
    investigation_id: str | None = None
    request_payload: str | None = None
    progress_summary: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    claim_token: str | None = None
    claimed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None

    @field_validator("created_at", "updated_at", "finished_at", "claimed_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamps must be timezone-aware")
        return value


class OperationAttempt(BaseModel):
    """A persisted ``operation_attempts`` row.

    One attempt row records each atomic claim of a queued operation: who claimed
    it (``claimed_by``), when the run started, and when the operation reached a
    terminal status.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str
    operation_id: str
    status: str
    claimed_by: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime

    @field_validator("started_at", "finished_at", "created_at")
    @classmethod
    def attempt_timestamps_must_be_timezone_aware(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamps must be timezone-aware")
        return value


class OperationAccepted(BaseModel):
    """The 202 body a route returns after enqueuing a durable operation.

    Carries only ``accepted=True`` and the new ``operation_id`` so a caller can
    follow the operation through the ``/api/v1/operations`` read surface without
    ever echoing a redacted payload or an actor identity back to the wire.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: Literal[True] = True
    operation_id: str


class OperationView(BaseModel):
    """The client-facing representation of an operation.

    Omits the redacted ``request_payload`` and the internal claim/identity
    columns so private request text and the actor identity never reach a client.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    kind: OperationKind
    status: OperationStatus
    target_id: str
    session_id: str | None
    investigation_id: str | None
    progress_summary: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None

    @field_validator("created_at", "updated_at", "finished_at")
    @classmethod
    def view_timestamps_must_be_timezone_aware(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamps must be timezone-aware")
        return value
