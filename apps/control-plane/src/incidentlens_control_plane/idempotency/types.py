"""Shared wire types for persistence-backed mutation idempotency.

The vocabulary here is deliberately small and stable: a record's ``state``
transitions from ``in_progress`` (reserved) to ``completed`` (2xx persisted),
and a single atomic reserve call reports one of the four outcomes the service
and routes act on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class IdempotencyState(StrEnum):
    """Lifecycle states of one idempotency reservation."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class ReservationStatus(StrEnum):
    """Outcome of one atomic reserve attempt.

    - ``RESERVED``: the key was newly reserved for this request.
    - ``REPLAY``: a completed 2xx with a matching request hash is being replayed.
    - ``CONFLICT``: the key was already completed with a different request hash.
    - ``IN_PROGRESS``: an unexpired reservation is still being executed.
    """

    RESERVED = "reserved"
    REPLAY = "replay"
    CONFLICT = "conflict"
    IN_PROGRESS = "in_progress"


@dataclass(frozen=True)
class Reservation:
    """Result of an atomic reserve, with replay payload when applicable."""

    status: ReservationStatus
    status_code: int | None = None
    response_json: str | None = None


@dataclass(frozen=True)
class IdempotencyRecord:
    """One persisted idempotency key row (database record)."""

    principal_id: str
    method: str
    route_key: str
    idempotency_key: str
    request_sha256: str
    state: IdempotencyState
    status_code: int | None
    response_json: str | None
    created_at: datetime
    completed_at: datetime | None
    expires_at: datetime
