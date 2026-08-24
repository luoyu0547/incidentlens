"""SQLite persistence for durable operations and their attempt history.

Follows the runtime.db / sqlite3 conventions of the sibling stores: an
idempotent ``migrate()`` inside a single explicit transaction, validated
Pydantic round trips, and conditional UPDATEs so status writes are atomic.
``claim`` moves one ``queued`` operation to ``running`` and records a fresh
attempt row with a SINGLE conditional UPDATE inside one transaction, so two
workers issuing the same claim race on the rowcount and only one wins — there
is no claim race even in a single-worker deployment.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from incidentlens_control_plane.operations.state_machine import (
    OPERATION_STATE_MACHINE,
    OPERATION_TERMINAL,
)
from incidentlens_control_plane.operations.types import (
    Operation,
    OperationAttempt,
    OperationKind,
    OperationStatus,
)


class OperationNotFound(Exception):
    """Raised when a requested operation has no persisted row."""


class OperationAlreadyExists(Exception):
    """Raised when creating an operation whose id already exists."""


class OperationNotClaimable(Exception):
    """Raised when an atomic claim matched no ``queued`` operation."""


class ConcurrentOperationUpdate(Exception):
    """Raised when a conditional status update matched no row."""


_OPERATION_COLUMNS = (
    "operation_id",
    "kind",
    "status",
    "target_id",
    "created_by",
    "session_id",
    "investigation_id",
    "request_payload",
    "progress_summary",
    "error_code",
    "error_message",
    "claim_token",
    "claimed_at",
    "created_at",
    "updated_at",
    "finished_at",
)

_ATTEMPT_COLUMNS = (
    "attempt_id",
    "operation_id",
    "status",
    "claimed_by",
    "started_at",
    "finished_at",
    "created_at",
)

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS operations (
        operation_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL
            CHECK (kind IN ('agent_message','target_test','investigation_start',
                            'rollback','report_generate')),
        status TEXT NOT NULL
            CHECK (status IN ('queued','running','cancel_requested','succeeded',
                              'failed','cancelled','uncertain')),
        target_id TEXT NOT NULL,
        created_by TEXT NOT NULL,
        session_id TEXT,
        investigation_id TEXT,
        request_payload TEXT,
        progress_summary TEXT,
        error_code TEXT,
        error_message TEXT,
        claim_token TEXT,
        claimed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS operation_attempts (
        attempt_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL
            REFERENCES operations(operation_id) ON DELETE CASCADE,
        status TEXT NOT NULL,
        claimed_by TEXT,
        started_at TEXT,
        finished_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_operations_claim
        ON operations(status, claimed_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_operations_queued
        ON operations(status, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_operations_session
        ON operations(session_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_operations_investigation
        ON operations(investigation_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_operation_attempts_operation
        ON operation_attempts(operation_id, created_at)
    """,
)


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _dt(value: object | None) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


class OperationStore:
    """SQLite-backed store for ``operations`` and ``operation_attempts``."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connection_factory = connection_factory

    def migrate(self) -> None:
        """Create both tables and their indexes in one transaction."""
        with self._connection_factory() as conn:
            conn.isolation_level = None
            conn.execute("BEGIN")
            try:
                for statement in _SCHEMA_STATEMENTS:
                    conn.execute(statement)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # -- reads -----------------------------------------------------------------

    def get(self, operation_id: str) -> Operation:
        """Return one operation, or raise OperationNotFound."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_OPERATION_COLUMNS)}
                FROM operations WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
        if row is None:
            raise OperationNotFound(f"operation not found: {operation_id}")
        return self._row_to_operation(row)

    def list_queued(self, *, limit: int = 100) -> tuple[Operation, ...]:
        """Return claimable (``queued``) operations, oldest first."""
        if not (1 <= limit <= 1000):
            raise ValueError("limit must be between 1 and 1000")
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_OPERATION_COLUMNS)}
                FROM operations
                WHERE status = 'queued'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(self._row_to_operation(row) for row in rows)

    def list_attempts(self, operation_id: str) -> tuple[OperationAttempt, ...]:
        """Return the attempt history of one operation, oldest first."""
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_ATTEMPT_COLUMNS)}
                FROM operation_attempts
                WHERE operation_id = ?
                ORDER BY created_at ASC
                """,
                (operation_id,),
            ).fetchall()
        return tuple(self._row_to_attempt(row) for row in rows)

    # -- writes ----------------------------------------------------------------

    def create(self, operation: Operation) -> Operation:
        """Persist a new operation; raise OperationAlreadyExists on a duplicate."""
        with self._connection_factory() as conn:
            try:
                conn.execute(
                    f"""
                    INSERT INTO operations ({", ".join(_OPERATION_COLUMNS)})
                    VALUES ({_placeholders(len(_OPERATION_COLUMNS))})
                    """,
                    self._operation_to_row(operation),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise OperationAlreadyExists(
                    f"operation already exists: {operation.operation_id}"
                ) from exc
        return operation

    def claim(
        self, operation_id: str, *, claim_token: str, now: datetime
    ) -> Operation:
        """Atomically move one ``queued`` operation to ``running``.

        The conditional UPDATE is the single atomic test-and-set: it matches only
        a ``queued`` row, so two concurrent claims race on the rowcount and only
        one wins.  A fresh ``operation_attempts`` row records the running attempt
        in the same transaction.  Raises ``OperationNotFound`` when the operation
        is gone and ``OperationNotClaimable`` when it is no longer ``queued``.
        """
        now_utc = now.astimezone(UTC)
        with self._connection_factory() as conn:
            conn.isolation_level = None
            conn.execute("BEGIN")
            try:
                cursor = conn.execute(
                    """
                    UPDATE operations
                    SET status = 'running', claim_token = ?, claimed_at = ?,
                        updated_at = ?
                    WHERE operation_id = ? AND status = 'queued'
                    """,
                    (claim_token, _iso(now_utc), _iso(now_utc), operation_id),
                )
                if cursor.rowcount == 0:
                    exists = conn.execute(
                        "SELECT operation_id FROM operations WHERE operation_id = ?",
                        (operation_id,),
                    ).fetchone()
                    if exists is None:
                        raise OperationNotFound(
                            f"operation not found: {operation_id}"
                        )
                    raise OperationNotClaimable(
                        f"operation {operation_id} is not claimable"
                    )
                attempt = OperationAttempt(
                    attempt_id=f"att_{uuid.uuid4().hex[:24]}",
                    operation_id=operation_id,
                    status="running",
                    claimed_by=claim_token,
                    started_at=now_utc,
                    finished_at=None,
                    created_at=now_utc,
                )
                conn.execute(
                    f"""
                    INSERT INTO operation_attempts ({", ".join(_ATTEMPT_COLUMNS)})
                    VALUES ({_placeholders(len(_ATTEMPT_COLUMNS))})
                    """,
                    self._attempt_to_row(attempt),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return self.get(operation_id)

    def transition(
        self,
        operation: Operation,
        target: OperationStatus,
        *,
        now: datetime,
        progress_summary: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> Operation:
        """Apply a state-machine-validated status transition atomically.

        The move is validated by the operation state machine before it is applied
        with a conditional UPDATE on the *current* status, so a concurrent writer
        cannot double-apply a transition.  Terminal moves also finalize the open
        running attempt and stamp ``finished_at``.  The returned ``Operation``
        always reflects the persisted row.
        """
        OPERATION_STATE_MACHINE.assert_transition(operation.status, target)
        now_utc = now.astimezone(UTC)
        finished_at = (
            now_utc if target in OPERATION_TERMINAL else operation.finished_at
        )
        with self._connection_factory() as conn:
            conn.isolation_level = None
            conn.execute("BEGIN")
            try:
                cursor = conn.execute(
                    """
                    UPDATE operations
                    SET status = ?,
                        progress_summary = COALESCE(?, progress_summary),
                        error_code = COALESCE(?, error_code),
                        error_message = COALESCE(?, error_message),
                        finished_at = ?, updated_at = ?
                    WHERE operation_id = ? AND status = ?
                    """,
                    (
                        target.value,
                        progress_summary,
                        error_code,
                        error_message,
                        _iso(finished_at) if finished_at is not None else None,
                        _iso(now_utc),
                        operation.operation_id,
                        operation.status.value,
                    ),
                )
                if cursor.rowcount == 0:
                    raise ConcurrentOperationUpdate(
                        f"operation {operation.operation_id} status changed concurrently"
                    )
                if target in OPERATION_TERMINAL:
                    conn.execute(
                        """
                        UPDATE operation_attempts
                        SET status = ?, finished_at = ?
                        WHERE operation_id = ? AND status = 'running'
                        """,
                        (target.value, _iso(now_utc), operation.operation_id),
                    )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return self.get(operation.operation_id)

    # -- row mapping -------------------------------------------------------------

    @staticmethod
    def _operation_to_row(operation: Operation) -> tuple[object, ...]:
        return (
            operation.operation_id,
            operation.kind.value,
            operation.status.value,
            operation.target_id,
            operation.created_by,
            operation.session_id,
            operation.investigation_id,
            operation.request_payload,
            operation.progress_summary,
            operation.error_code,
            operation.error_message,
            operation.claim_token,
            _iso(operation.claimed_at) if operation.claimed_at is not None else None,
            _iso(operation.created_at),
            _iso(operation.updated_at),
            _iso(operation.finished_at) if operation.finished_at is not None else None,
        )

    @staticmethod
    def _row_to_operation(row: tuple[object, ...]) -> Operation:
        return Operation(
            operation_id=str(row[0]),
            kind=OperationKind(str(row[1])),
            status=OperationStatus(str(row[2])),
            target_id=str(row[3]),
            created_by=str(row[4]),
            session_id=str(row[5]) if row[5] is not None else None,
            investigation_id=str(row[6]) if row[6] is not None else None,
            request_payload=str(row[7]) if row[7] is not None else None,
            progress_summary=str(row[8]) if row[8] is not None else None,
            error_code=str(row[9]) if row[9] is not None else None,
            error_message=str(row[10]) if row[10] is not None else None,
            claim_token=str(row[11]) if row[11] is not None else None,
            claimed_at=_dt(row[12]),
            created_at=_dt(row[13]),  # type: ignore[arg-type]
            updated_at=_dt(row[14]),  # type: ignore[arg-type]
            finished_at=_dt(row[15]),
        )

    @staticmethod
    def _attempt_to_row(attempt: OperationAttempt) -> tuple[object, ...]:
        return (
            attempt.attempt_id,
            attempt.operation_id,
            attempt.status,
            attempt.claimed_by,
            _iso(attempt.started_at) if attempt.started_at is not None else None,
            _iso(attempt.finished_at) if attempt.finished_at is not None else None,
            _iso(attempt.created_at),
        )

    @staticmethod
    def _row_to_attempt(row: tuple[object, ...]) -> OperationAttempt:
        return OperationAttempt(
            attempt_id=str(row[0]),
            operation_id=str(row[1]),
            status=str(row[2]),
            claimed_by=str(row[3]) if row[3] is not None else None,
            started_at=_dt(row[4]),
            finished_at=_dt(row[5]),
            created_at=_dt(row[6]),  # type: ignore[arg-type]
        )


__all__ = [
    "ConcurrentOperationUpdate",
    "OperationAlreadyExists",
    "OperationNotClaimable",
    "OperationNotFound",
    "OperationStore",
]
