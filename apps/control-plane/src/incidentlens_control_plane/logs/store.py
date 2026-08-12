import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from incidentlens_control_plane.logs.types import (
    InvalidSubscriptionTransition,
    LogCursor,
    LogRecord,
    LogScope,
    LogSeverity,
    LogSourceKind,
    LogSubscription,
    LogSubscriptionStatus,
)


class LogSearchFilters(BaseModel):
    """Filters applied to log record search. `text` runs an FTS5 full-text match."""

    model_config = ConfigDict(extra="forbid")
    project_id: str | None = None
    target_id: str | None = None
    service_name: str | None = None
    source_kind: LogSourceKind | None = None
    scope: LogScope | None = None
    severity: LogSeverity | None = None
    text: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None


_RECORD_COLUMNS = (
    "log_id",
    "subscription_id",
    "project_id",
    "target_id",
    "service_name",
    "source_kind",
    "scope",
    "source_ref",
    "cursor",
    "dedupe_key",
    "observed_at",
    "event_time",
    "severity",
    "message_redacted",
    "redaction_summary_json",
    "normal_signal",
    "correlation_key",
    "evidence_ref_id",
    "created_at",
)

_SUBSCRIPTION_COLUMNS = (
    "subscription_id",
    "project_id",
    "target_id",
    "service_name",
    "source_kind",
    "scope",
    "source_ref",
    "opt_in_streaming",
    "status",
    "created_by",
    "last_error",
    "last_error_redacted",
    "created_at",
    "updated_at",
)

_CURSOR_COLUMNS = (
    "subscription_id",
    "cursor",
    "generation",
    "observed_at",
    "updated_at",
)


def _fts_query(text: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_./:-]+", text)
    if not tokens:
        raise ValueError("search text must contain at least one token")
    return " ".join(f'"{token}"' for token in tokens[:8])


def _record_from_row(row: tuple[object, ...]) -> LogRecord:
    return LogRecord(
        log_id=row[0],
        subscription_id=row[1],
        project_id=row[2],
        target_id=row[3],
        service_name=row[4],
        source_kind=LogSourceKind(row[5]),
        scope=LogScope(row[6]),
        source_ref=row[7],
        cursor=row[8],
        dedupe_key=row[9],
        observed_at=datetime.fromisoformat(row[10]),
        event_time=datetime.fromisoformat(row[11]) if row[11] is not None else None,
        severity=LogSeverity(row[12]),
        message_redacted=row[13],
        redaction_summary=json.loads(row[14]),
        normal_signal=row[15],
        correlation_key=row[16],
        evidence_ref_id=row[17],
        created_at=datetime.fromisoformat(row[18]),
    )


def _subscription_from_row(row: tuple[object, ...]) -> LogSubscription:
    return LogSubscription(
        subscription_id=row[0],
        project_id=row[1],
        target_id=row[2],
        service_name=row[3],
        source_kind=LogSourceKind(row[4]),
        scope=LogScope(row[5]),
        source_ref=row[6],
        opt_in_streaming=bool(row[7]),
        status=LogSubscriptionStatus(row[8]),
        created_by=row[9],
        last_error=row[10],
        last_error_redacted=row[11],
        created_at=datetime.fromisoformat(row[12]),
        updated_at=datetime.fromisoformat(row[13]),
    )


def _cursor_from_row(row: tuple[object, ...]) -> LogCursor:
    return LogCursor(
        subscription_id=row[0],
        cursor=row[1],
        generation=row[2],
        observed_at=datetime.fromisoformat(row[3]) if row[3] is not None else None,
        updated_at=datetime.fromisoformat(row[4]),
    )


class LogStore:
    """SQLite persistence for redacted log records, subscriptions, cursors, and runs."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connection_factory = connection_factory

    def migrate(self) -> None:
        """Create the log tables and FTS index if they don't exist."""
        with self._connection_factory() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS log_records (
                    log_id TEXT PRIMARY KEY,
                    subscription_id TEXT,
                    project_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    observed_at TEXT NOT NULL,
                    event_time TEXT,
                    severity TEXT NOT NULL,
                    message_redacted TEXT NOT NULL,
                    redaction_summary_json TEXT NOT NULL,
                    normal_signal TEXT,
                    correlation_key TEXT,
                    evidence_ref_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS log_records_fts
                USING fts5(log_id UNINDEXED, message_redacted);

                CREATE TABLE IF NOT EXISTS log_subscriptions (
                    subscription_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    opt_in_streaming INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    last_error TEXT,
                    last_error_redacted TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS log_cursors (
                    subscription_id TEXT PRIMARY KEY,
                    cursor TEXT NOT NULL,
                    generation TEXT,
                    observed_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS log_subscription_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    stopped_at TEXT,
                    status TEXT,
                    records_seen INTEGER,
                    error TEXT
                );
                """
            )
            conn.commit()
        self._ensure_subscription_columns()

    def _ensure_subscription_columns(self) -> None:
        """Add the ``last_error_redacted`` column to an existing schema.

        Fresh databases get the column from the CREATE TABLE statement; older
        databases need an idempotent ALTER TABLE so ``mark_subscription_error``
        can persist redacted summaries.
        """
        with self._connection_factory() as conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(log_subscriptions)")
            }
            if "last_error_redacted" not in columns:
                conn.execute(
                    "ALTER TABLE log_subscriptions ADD COLUMN last_error_redacted TEXT"
                )
                conn.commit()

    def append_batch(self, records: tuple[LogRecord, ...]) -> tuple[LogRecord, ...]:
        """Insert records, deduping by dedupe_key, and mirror new rows into FTS.

        The log_records and log_records_fts writes share one transaction so the
        two tables stay consistent. Only records that were newly inserted (the
        INSERT OR IGNORE affected exactly one row) are mirrored into FTS.
        """
        with self._connection_factory() as conn:
            inserted: list[LogRecord] = []
            for record in records:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO log_records (
                        log_id, subscription_id, project_id, target_id, service_name,
                        source_kind, scope, source_ref, cursor, dedupe_key,
                        observed_at, event_time, severity, message_redacted,
                        redaction_summary_json, normal_signal, correlation_key,
                        evidence_ref_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.log_id,
                        record.subscription_id,
                        record.project_id,
                        record.target_id,
                        record.service_name,
                        record.source_kind.value,
                        record.scope.value,
                        record.source_ref,
                        record.cursor,
                        record.dedupe_key,
                        record.observed_at.isoformat(),
                        record.event_time.isoformat() if record.event_time is not None else None,
                        record.severity.value,
                        record.message_redacted,
                        json.dumps(record.redaction_summary),
                        record.normal_signal,
                        record.correlation_key,
                        record.evidence_ref_id,
                        record.created_at.isoformat(),
                    ),
                )
                if cursor.rowcount == 1:
                    conn.execute(
                        "INSERT INTO log_records_fts (log_id, message_redacted) VALUES (?, ?)",
                        (record.log_id, record.message_redacted),
                    )
                    inserted.append(record)
            conn.commit()
            return tuple(inserted)

    def records_by_dedupe_keys(self, keys: tuple[str, ...]) -> tuple[LogRecord, ...]:
        """Return the stored records for the given dedupe keys, in ``keys`` order.

        Used to return store-consistent results after ``append_batch`` (which
        dedupes by ``dedupe_key``), so re-polled queries see the stored rows
        with their stable ``log_id`` values rather than freshly generated ones.
        """
        if not keys:
            return ()
        placeholders = ", ".join("?" for _ in keys)
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_RECORD_COLUMNS)}
                FROM log_records
                WHERE dedupe_key IN ({placeholders})
                """,
                tuple(keys),
            ).fetchall()
        found = {record.dedupe_key: record for record in map(_record_from_row, rows)}
        return tuple(found[key] for key in keys)

    def get_record(self, log_id: str) -> LogRecord | None:
        """Return the stored record with the given ``log_id``, or None."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_RECORD_COLUMNS)}
                FROM log_records
                WHERE log_id = ?
                """,
                (log_id,),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def list_records_for_subscription(
        self,
        subscription_id: str,
        after_cursor: str | None = None,
        limit: int = 1000,
    ) -> tuple[LogRecord, ...]:
        """Return records for a subscription in cursor order, optionally after a cursor.

        ``after_cursor`` is a lower bound on ``cursor`` for pagination; ``None``
        means no lower bound (start from the first record).
        """
        if not (1 <= limit <= 1000):
            raise ValueError("limit must be between 1 and 1000")

        clauses = ["subscription_id = ?"]
        params: list[object] = [subscription_id]
        if after_cursor is not None:
            clauses.append("cursor > ?")
            params.append(after_cursor)

        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_RECORD_COLUMNS)}
                FROM log_records
                WHERE {" AND ".join(clauses)}
                ORDER BY cursor ASC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def search(self, filters: LogSearchFilters, limit: int = 100) -> tuple[LogRecord, ...]:
        """Search log records, optionally restricting by filters and an FTS text match."""
        if not (1 <= limit <= 1000):
            raise ValueError("limit must be between 1 and 1000")

        clauses: list[str] = []
        params: list[object] = []

        if filters.project_id is not None:
            clauses.append("project_id = ?")
            params.append(filters.project_id)
        if filters.target_id is not None:
            clauses.append("target_id = ?")
            params.append(filters.target_id)
        if filters.service_name is not None:
            clauses.append("service_name = ?")
            params.append(filters.service_name)
        if filters.source_kind is not None:
            clauses.append("source_kind = ?")
            params.append(filters.source_kind.value)
        if filters.scope is not None:
            clauses.append("scope = ?")
            params.append(filters.scope.value)
        if filters.severity is not None:
            clauses.append("severity = ?")
            params.append(filters.severity.value)
        if filters.start_time is not None:
            clauses.append("observed_at >= ?")
            params.append(filters.start_time.isoformat())
        if filters.end_time is not None:
            clauses.append("observed_at <= ?")
            params.append(filters.end_time.isoformat())
        if filters.text is not None and filters.text.strip():
            clauses.append(
                "log_id IN (SELECT log_id FROM log_records_fts WHERE log_records_fts MATCH ?)"
            )
            params.append(_fts_query(filters.text))

        where_sql = " AND ".join(clauses)
        if where_sql:
            where_sql = f"WHERE {where_sql}"

        with self._connection_factory() as conn:
            cursor = conn.execute(
                f"""
                SELECT {", ".join(_RECORD_COLUMNS)}
                FROM log_records
                {where_sql}
                ORDER BY observed_at DESC
                LIMIT ?
                """,
                (*params, limit),
            )
            return tuple(_record_from_row(row) for row in cursor.fetchall())

    def create_subscription(
        self,
        project_id: str,
        target_id: str,
        service_name: str,
        source_kind: LogSourceKind,
        scope: LogScope,
        source_ref: str,
        opt_in_streaming: bool,
        created_by: str,
        now: datetime,
    ) -> LogSubscription:
        subscription = LogSubscription(
            subscription_id=f"sub-{uuid.uuid4().hex[:12]}",
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            source_kind=source_kind,
            scope=scope,
            source_ref=source_ref,
            opt_in_streaming=opt_in_streaming,
            status=LogSubscriptionStatus.ACTIVE,
            created_by=created_by,
            last_error=None,
            last_error_redacted=None,
            created_at=now,
            updated_at=now,
        )
        with self._connection_factory() as conn:
            conn.execute(
                f"""
                INSERT INTO log_subscriptions ({", ".join(_SUBSCRIPTION_COLUMNS)})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subscription.subscription_id,
                    subscription.project_id,
                    subscription.target_id,
                    subscription.service_name,
                    subscription.source_kind.value,
                    subscription.scope.value,
                    subscription.source_ref,
                    int(subscription.opt_in_streaming),
                    subscription.status.value,
                    subscription.created_by,
                    subscription.last_error,
                    subscription.last_error_redacted,
                    subscription.created_at.isoformat(),
                    subscription.updated_at.isoformat(),
                ),
            )
            conn.commit()
        return subscription

    def pause_subscription(self, subscription_id: str, now: datetime) -> LogSubscription:
        return self._transition_status(
            subscription_id,
            LogSubscriptionStatus.PAUSED,
            now,
            allowed_from=(LogSubscriptionStatus.ACTIVE,),
        )

    def resume_subscription(self, subscription_id: str, now: datetime) -> LogSubscription:
        return self._transition_status(
            subscription_id,
            LogSubscriptionStatus.ACTIVE,
            now,
            allowed_from=(
                LogSubscriptionStatus.PAUSED,
                LogSubscriptionStatus.ERROR,
            ),
        )

    def delete_subscription(self, subscription_id: str, now: datetime) -> LogSubscription:
        return self._set_subscription_status(subscription_id, LogSubscriptionStatus.DELETED, now)

    def _transition_status(
        self,
        subscription_id: str,
        status: LogSubscriptionStatus,
        now: datetime,
        allowed_from: tuple[LogSubscriptionStatus, ...],
    ) -> LogSubscription:
        """Atomically move a subscription from an allowed status to ``status``.

        The conditional UPDATE makes the check-and-set atomic, so no
        check-then-write race can resurrect or mis-transition a row.  Raises
        ``KeyError`` when the subscription does not exist and
        ``InvalidSubscriptionTransition`` when its current status is not one of
        ``allowed_from``.
        """
        placeholders = ", ".join("?" for _ in allowed_from)
        with self._connection_factory() as conn:
            cursor = conn.execute(
                f"""
                UPDATE log_subscriptions
                SET status = ?, updated_at = ?
                WHERE subscription_id = ? AND status IN ({placeholders})
                """,
                (
                    status.value,
                    now.isoformat(),
                    subscription_id,
                    *(entry.value for entry in allowed_from),
                ),
            )
            conn.commit()
            if cursor.rowcount == 0:
                row = conn.execute(
                    f"""
                    SELECT {", ".join(_SUBSCRIPTION_COLUMNS)}
                    FROM log_subscriptions
                    WHERE subscription_id = ?
                    """,
                    (subscription_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"subscription not found: {subscription_id}")
                current = _subscription_from_row(row)
                raise InvalidSubscriptionTransition(
                    f"cannot transition subscription {subscription_id} "
                    f"from {current.status.value} to {status.value}"
                )
            row = conn.execute(
                f"""
                SELECT {", ".join(_SUBSCRIPTION_COLUMNS)}
                FROM log_subscriptions
                WHERE subscription_id = ?
                """,
                (subscription_id,),
            ).fetchone()
        assert row is not None
        return _subscription_from_row(row)

    def _set_subscription_status(
        self, subscription_id: str, status: LogSubscriptionStatus, now: datetime
    ) -> LogSubscription:
        with self._connection_factory() as conn:
            conn.execute(
                """
                UPDATE log_subscriptions
                SET status = ?, updated_at = ?
                WHERE subscription_id = ?
                """,
                (status.value, now.isoformat(), subscription_id),
            )
            conn.commit()
        updated = self.get_subscription(subscription_id)
        if updated is None:
            raise KeyError(f"subscription not found: {subscription_id}")
        return updated

    def mark_subscription_error(
        self, subscription_id: str, last_error_redacted: str, now: datetime
    ) -> LogSubscription | None:
        """Mark an EXISTING subscription errored; no-op when the row is absent.

        Only the *redacted* error summary is persisted; the raw error text never
        reaches the database.  The update targets an existing row only and never
        fabricates or resurrects a subscription, so a deleted or never-created
        id stays deleted/absent.  Returns the updated subscription, or None when
        no row matched.
        """
        with self._connection_factory() as conn:
            conn.execute(
                """
                UPDATE log_subscriptions
                SET status = ?, last_error_redacted = ?, updated_at = ?
                WHERE subscription_id = ?
                """,
                (
                    LogSubscriptionStatus.ERROR.value,
                    last_error_redacted,
                    now.isoformat(),
                    subscription_id,
                ),
            )
            conn.commit()
        return self.get_subscription(subscription_id)

    def get_subscription(self, subscription_id: str) -> LogSubscription | None:
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_SUBSCRIPTION_COLUMNS)}
                FROM log_subscriptions
                WHERE subscription_id = ?
                """,
                (subscription_id,),
            ).fetchone()
        return _subscription_from_row(row) if row is not None else None

    def list_subscriptions(
        self,
        *,
        project_id: str | None = None,
        target_id: str | None = None,
        service_name: str | None = None,
        status: LogSubscriptionStatus | None = None,
        opt_in_streaming: bool | None = None,
    ) -> tuple[LogSubscription, ...]:
        clauses: list[str] = []
        params: list[object] = []

        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if target_id is not None:
            clauses.append("target_id = ?")
            params.append(target_id)
        if service_name is not None:
            clauses.append("service_name = ?")
            params.append(service_name)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if opt_in_streaming is not None:
            clauses.append("opt_in_streaming = ?")
            params.append(int(opt_in_streaming))

        where_sql = " AND ".join(clauses)
        if where_sql:
            where_sql = f"WHERE {where_sql}"

        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_SUBSCRIPTION_COLUMNS)}
                FROM log_subscriptions
                {where_sql}
                ORDER BY created_at ASC
                """,
                tuple(params),
            ).fetchall()
        return tuple(_subscription_from_row(row) for row in rows)

    def list_active_opt_in_subscriptions(self) -> tuple[LogSubscription, ...]:
        return self.list_subscriptions(
            status=LogSubscriptionStatus.ACTIVE, opt_in_streaming=True
        )

    def upsert_cursor(
        self,
        subscription_id: str,
        cursor: str,
        generation: str | None,
        observed_at: datetime | None,
        now: datetime,
    ) -> LogCursor:
        log_cursor = LogCursor(
            subscription_id=subscription_id,
            cursor=cursor,
            generation=generation,
            observed_at=observed_at,
            updated_at=now,
        )
        with self._connection_factory() as conn:
            conn.execute(
                f"""
                INSERT INTO log_cursors ({", ".join(_CURSOR_COLUMNS)})
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(subscription_id) DO UPDATE SET
                    cursor = excluded.cursor,
                    generation = excluded.generation,
                    observed_at = excluded.observed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    log_cursor.subscription_id,
                    log_cursor.cursor,
                    log_cursor.generation,
                    (
                        log_cursor.observed_at.isoformat()
                        if log_cursor.observed_at is not None
                        else None
                    ),
                    log_cursor.updated_at.isoformat(),
                ),
            )
            conn.commit()
        return log_cursor

    def get_cursor(self, subscription_id: str) -> LogCursor | None:
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_CURSOR_COLUMNS)}
                FROM log_cursors
                WHERE subscription_id = ?
                """,
                (subscription_id,),
            ).fetchone()
        return _cursor_from_row(row) if row is not None else None

    def record_run_start(self, subscription_id: str, started_at: datetime) -> int:
        """Record the start of a subscription run, returning the new run id."""
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                INSERT INTO log_subscription_runs (subscription_id, started_at, status)
                VALUES (?, ?, ?)
                """,
                (subscription_id, started_at.isoformat(), "running"),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def record_run_stop(
        self,
        run_id: int,
        stopped_at: datetime,
        *,
        status: str = "completed",
        records_seen: int = 0,
        error: str | None = None,
    ) -> None:
        """Record the completion of a subscription run."""
        with self._connection_factory() as conn:
            conn.execute(
                """
                UPDATE log_subscription_runs
                SET stopped_at = ?, status = ?, records_seen = ?, error = ?
                WHERE run_id = ?
                """,
                (stopped_at.isoformat(), status, records_seen, error, run_id),
            )
            conn.commit()
