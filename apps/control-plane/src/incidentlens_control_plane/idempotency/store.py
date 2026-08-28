"""SQLite-backed idempotency store with atomic reservation.

The ``api_idempotency_keys`` table is keyed by
``(principal_id, method, route_key, idempotency_key)`` so idempotency is scoped
to a single authenticated principal and a single endpoint.  A reservation is an
atomic insert of a single ``in_progress`` row inside one ``BEGIN IMMEDIATE``
transaction; concurrent occupants (or a row from an earlier request) surface
through their state and request hash, letting the caller replay a completed 2xx,
reject a hash collision, or report a still-running sibling.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from incidentlens_control_plane.idempotency.types import (
    IdempotencyRecord,
    IdempotencyState,
    Reservation,
    ReservationStatus,
)

#: Lease on a fresh in_progress reservation.  It is deliberately longer than the
#: failure re-arm lease so a legitimate slow mutation retried, say, 90 seconds
#: in does not double-execute: the occupant is only reclaimable once the worker
#: exceeds this window.
RESERVATION_LEASE_SECONDS = 300

#: Re-arm lease for a failed (non-2xx / raised) reservation.  A worker that
#: crashes or fails leaves the key reclaimable after this many seconds, so a
#: same-key retry does not deadlock on a reservation that will never complete.
FAILED_RESERVATION_LEASE_SECONDS = 60

#: How long a completed response is retained for exact replay.
COMPLETED_RETENTION_SECONDS = 24 * 60 * 60

_TABLE = "api_idempotency_keys"

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS api_idempotency_keys (
        principal_id TEXT NOT NULL,
        method TEXT NOT NULL,
        route_key TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('in_progress', 'completed')),
        status_code INTEGER,
        response_json TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (principal_id, method, route_key, idempotency_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_api_idempotency_expiry
        ON api_idempotency_keys(expires_at)
    """,
)

#: Columns that a future release may add; ``migrate`` appends any that are
#: missing so an existing ``runtime.db`` upgrades additively and idempotently.
_FORWARD_COLUMNS: dict[str, str] = {}


class IdempotencyStore:
    """SQLite persistence for idempotent mutation reservations."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connection_factory = connection_factory

    def migrate(self) -> None:
        """Create the idempotency table and expiry index, additively.

        ``CREATE TABLE IF NOT EXISTS`` plus ``CREATE INDEX IF NOT EXISTS`` make
        the migration a no-op on re-runs; the forward-column pass appends any
        column a future release added without destroying existing rows.
        """
        with self._connection_factory() as conn:
            conn.isolation_level = None
            conn.execute("BEGIN")
            try:
                for statement in _SCHEMA_STATEMENTS:
                    conn.execute(statement)
                for column, default in _FORWARD_COLUMNS.items():
                    if self._table_missing_column(conn, column):
                        conn.execute(
                            f"ALTER TABLE {_TABLE} ADD COLUMN {column} {default}"
                        )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    @classmethod
    def _table_missing_column(cls, conn: sqlite3.Connection, column: str) -> bool:
        if not cls._table_exists(conn, _TABLE):
            return False
        present = {
            row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")
        }
        return column not in present

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
        """Atomically reserve *idempotency_key* for this principal and endpoint.

        Runs in one ``BEGIN IMMEDIATE`` transaction: expired rows are pruned,
        then the key is inserted as ``in_progress``.  If the insert wins, the
        caller owns the reservation; otherwise the pre-existing row is returned
        as a replayable / conflicting / still-running outcome.
        """
        now_utc = now.astimezone(UTC)
        with self._connection_factory() as conn:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "DELETE FROM api_idempotency_keys WHERE expires_at <= ?",
                    (now_utc.isoformat(),),
                )
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO api_idempotency_keys
                        (principal_id, method, route_key, idempotency_key,
                         request_sha256, state, status_code, response_json,
                         created_at, completed_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, 'in_progress', NULL, NULL, ?, NULL, ?)
                    """,
                    (
                        principal_id,
                        method,
                        route_key,
                        idempotency_key,
                        request_sha256,
                        now_utc.isoformat(),
                        (
                            now_utc
                            + timedelta(seconds=RESERVATION_LEASE_SECONDS)
                        ).isoformat(),
                    ),
                )
                if cursor.rowcount == 1:
                    conn.execute("COMMIT")
                    return Reservation(status=ReservationStatus.RESERVED)
                row = conn.execute(
                    """
                    SELECT request_sha256, state, status_code, response_json
                    FROM api_idempotency_keys
                    WHERE principal_id = ? AND method = ? AND route_key = ?
                      AND idempotency_key = ?
                    """,
                    (principal_id, method, route_key, idempotency_key),
                ).fetchone()
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        if row is None:
            raise RuntimeError("idempotency reservation lost its occupant")
        return self._existing_reservation(row, request_sha256)

    def mark_completed(
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
        now_utc = now.astimezone(UTC)
        with self._connection_factory() as conn:
            conn.execute(
                """
                UPDATE api_idempotency_keys
                SET state = 'completed', status_code = ?, response_json = ?,
                    completed_at = ?, expires_at = ?
                WHERE principal_id = ? AND method = ? AND route_key = ?
                  AND idempotency_key = ? AND state = 'in_progress'
                """,
                (
                    status_code,
                    response_json,
                    now_utc.isoformat(),
                    (
                        now_utc
                        + timedelta(seconds=COMPLETED_RETENTION_SECONDS)
                    ).isoformat(),
                    principal_id,
                    method,
                    route_key,
                    idempotency_key,
                ),
            )
            conn.commit()

    def rearm_lease(
        self,
        *,
        principal_id: str,
        method: str,
        route_key: str,
        idempotency_key: str,
        now: datetime,
    ) -> None:
        """Re-arm a failed 5xx reservation with a fresh short lease.

        The row stays ``in_progress`` (never replayed as success) but becomes
        reclaimable about 60 seconds out, so a later same-key retry can run the
        mutation again instead of returning ``idempotency_in_progress`` forever.
        """
        now_utc = now.astimezone(UTC)
        with self._connection_factory() as conn:
            conn.execute(
                """
                UPDATE api_idempotency_keys
                SET expires_at = ?
                WHERE principal_id = ? AND method = ? AND route_key = ?
                  AND idempotency_key = ? AND state = 'in_progress'
                """,
                (
                    (
                        now_utc
                        + timedelta(seconds=FAILED_RESERVATION_LEASE_SECONDS)
                    ).isoformat(),
                    principal_id,
                    method,
                    route_key,
                    idempotency_key,
                ),
            )
            conn.commit()

    def get(
        self,
        *,
        principal_id: str,
        method: str,
        route_key: str,
        idempotency_key: str,
    ) -> IdempotencyRecord | None:
        """Return the persisted record for one key, or ``None``."""
        with self._connection_factory() as conn:
            row = conn.execute(
                """
                SELECT principal_id, method, route_key, idempotency_key,
                       request_sha256, state, status_code, response_json,
                       created_at, completed_at, expires_at
                FROM api_idempotency_keys
                WHERE principal_id = ? AND method = ? AND route_key = ?
                  AND idempotency_key = ?
                """,
                (principal_id, method, route_key, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        return IdempotencyRecord(
            principal_id=str(row[0]),
            method=str(row[1]),
            route_key=str(row[2]),
            idempotency_key=str(row[3]),
            request_sha256=str(row[4]),
            state=IdempotencyState(str(row[5])),
            status_code=int(row[6]) if row[6] is not None else None,
            response_json=str(row[7]) if row[7] is not None else None,
            created_at=datetime.fromisoformat(str(row[8])),
            completed_at=datetime.fromisoformat(str(row[9])) if row[9] else None,
            expires_at=datetime.fromisoformat(str(row[10])),
        )

    def prune_expired(self, now: datetime) -> int:
        """Delete rows whose ``expires_at`` has passed; return the row count."""
        now_utc = now.astimezone(UTC)
        with self._connection_factory() as conn:
            cursor = conn.execute(
                "DELETE FROM api_idempotency_keys WHERE expires_at <= ?",
                (now_utc.isoformat(),),
            )
            conn.commit()
        return cursor.rowcount

    @staticmethod
    def _existing_reservation(row: sqlite3.Row, request_sha256: str) -> Reservation:
        state = IdempotencyState(str(row[1]))
        if state == IdempotencyState.COMPLETED:
            if str(row[0]) == request_sha256:
                return Reservation(
                    status=ReservationStatus.REPLAY,
                    status_code=int(row[2]) if row[2] is not None else None,
                    response_json=str(row[3]) if row[3] is not None else None,
                )
            return Reservation(status=ReservationStatus.CONFLICT)
        return Reservation(status=ReservationStatus.IN_PROGRESS)
