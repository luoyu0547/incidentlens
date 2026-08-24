"""Service facade for the idempotency store.

The service is deliberately thin: it owns the store reference and exposes the
reserve / complete / re-arm lifecycle verbs that ``execute_idempotent`` (in
``api/idempotency.py``) composes into the exact-replay contract.  Keeping the
SQL and the orchestration separate lets unit tests drive the store directly
while route tests exercise the full helper.
"""

from __future__ import annotations

from datetime import datetime

from incidentlens_control_plane.idempotency.store import IdempotencyStore
from incidentlens_control_plane.idempotency.types import (
    IdempotencyRecord,
    Reservation,
)


class IdempotencyService:
    """Coordinates the persisted lifecycle of one idempotent mutation."""

    def __init__(self, store: IdempotencyStore) -> None:
        self._store = store

    def reserve(
        self,
        *,
        principal_id: str,
        method: str,
        route_key: str,
        idempotency_key: str,
        request_sha256: str,
        now: datetime,
    ) -> Reservation:
        """Atomically reserve *idempotency_key* (see store docstring)."""
        return self._store.reserve(
            principal_id=principal_id,
            method=method,
            route_key=route_key,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            now=now,
        )

    def complete(
        self,
        *,
        principal_id: str,
        method: str,
        route_key: str,
        idempotency_key: str,
        status_code: int,
        response_json: str,
        now: datetime,
    ) -> None:
        """Persist a successful 2xx response for later exact replay."""
        self._store.mark_completed(
            principal_id=principal_id,
            method=method,
            route_key=route_key,
            idempotency_key=idempotency_key,
            status_code=status_code,
            response_json=response_json,
            now=now,
        )

    def keep_alive_after_failure(
        self,
        *,
        principal_id: str,
        method: str,
        route_key: str,
        idempotency_key: str,
        now: datetime,
    ) -> None:
        """Re-arm a failed 5xx reservation with a fresh short lease."""
        self._store.rearm_lease(
            principal_id=principal_id,
            method=method,
            route_key=route_key,
            idempotency_key=idempotency_key,
            now=now,
        )

    def get(
        self,
        *,
        principal_id: str,
        method: str,
        route_key: str,
        idempotency_key: str,
    ) -> IdempotencyRecord | None:
        """Return the persisted record for one key, or ``None``."""
        return self._store.get(
            principal_id=principal_id,
            method=method,
            route_key=route_key,
            idempotency_key=idempotency_key,
        )

    @property
    def store(self) -> IdempotencyStore:
        """The underlying store (exposed for tests and introspection)."""
        return self._store
