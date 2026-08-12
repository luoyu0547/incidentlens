"""Append-only Evidence Store over redacted, bounded content.

The Evidence Store is immutable: evidence refs are created once and never
updated or deleted. Content hashes are computed exclusively over the already
redacted (and, where over-limit, truncated) content so raw content never
reaches this store.  Log-specific columns are nullable and populated only for
``log_record`` refs; ``dedupe_key`` is the non-null idempotency identity shared
by the source identity plus the content hash.

``migrate()`` upgrades a legacy pre-Phase-4 schema in place: the old table is
renamed, the unified schema is created, and existing log rows are copied over
with a derived ``dedupe_key`` and defaults for the new columns.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime

from incidentlens_control_plane.evidence.types import (
    EvidenceKind,
    EvidenceRef,
)
from incidentlens_control_plane.logs.types import (
    LogRecord,
    LogScope,
    LogSeverity,
    LogSourceKind,
    TruncationInfo,
)

_EVIDENCE_COLUMNS = (
    "evidence_ref_id",
    "incident_id",
    "evidence_kind",
    "agent_run_id",
    "project_id",
    "target_id",
    "service_name",
    "source_ref",
    "source_kind",
    "scope",
    "cursor",
    "content_redacted",
    "content_sha256",
    "redaction_summary_json",
    "truncation_json",
    "metadata_json",
    "severity",
    "event_time",
    "normal_signal",
    "correlation_key",
    "dedupe_key",
    "created_at",
    "created_by",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evidence_refs (
    evidence_ref_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    agent_run_id TEXT,
    project_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    service_name TEXT NOT NULL,
    source_ref TEXT,
    source_kind TEXT,
    scope TEXT,
    cursor TEXT,
    content_redacted TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    redaction_summary_json TEXT NOT NULL,
    truncation_json TEXT,
    metadata_json TEXT NOT NULL,
    severity TEXT,
    event_time TEXT,
    normal_signal TEXT,
    correlation_key TEXT,
    dedupe_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_evidence_refs_incident
    ON evidence_refs(incident_id, created_at);
CREATE INDEX IF NOT EXISTS idx_evidence_refs_run
    ON evidence_refs(agent_run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_evidence_refs_kind
    ON evidence_refs(evidence_kind, created_at);
CREATE INDEX IF NOT EXISTS idx_evidence_refs_incident_kind
    ON evidence_refs(incident_id, evidence_kind, created_at);
"""

_LEGACY_COLUMNS = (
    "evidence_ref_id",
    "incident_id",
    "evidence_kind",
    "project_id",
    "target_id",
    "service_name",
    "source_kind",
    "scope",
    "source_ref",
    "cursor",
    "content_redacted",
    "content_sha256",
    "redaction_summary_json",
    "severity",
    "event_time",
    "normal_signal",
    "correlation_key",
    "created_at",
    "created_by",
)


def _derive_dedupe_key(evidence: EvidenceRef) -> str:
    """Return the canonical idempotency key for an evidence ref.

    The key is derived from the kind, the generic source identity and the hash
    of the redacted content, so re-creating the same evidence yields the same
    key and never duplicates rows regardless of which log-specific columns are
    NULL.
    """
    identity = "|".join(
        (
            evidence.evidence_kind.value,
            evidence.agent_run_id or "",
            evidence.project_id,
            evidence.target_id,
            evidence.service_name,
            evidence.source_ref or "",
            evidence.source_kind.value if evidence.source_kind is not None else "",
            evidence.scope.value if evidence.scope is not None else "",
            evidence.cursor or "",
        )
    )
    return hashlib.sha256(
        f"{identity}|{evidence.content_sha256}".encode("utf-8")
    ).hexdigest()


def _evidence_values(evidence: EvidenceRef) -> tuple[object, ...]:
    return (
        evidence.evidence_ref_id,
        evidence.incident_id,
        evidence.evidence_kind.value,
        evidence.agent_run_id,
        evidence.project_id,
        evidence.target_id,
        evidence.service_name,
        evidence.source_ref,
        (
            evidence.source_kind.value
            if evidence.source_kind is not None
            else None
        ),
        evidence.scope.value if evidence.scope is not None else None,
        evidence.cursor,
        evidence.content_redacted,
        evidence.content_sha256,
        json.dumps(evidence.redaction_summary),
        (
            evidence.truncation.model_dump_json()
            if evidence.truncation is not None
            else None
        ),
        json.dumps(evidence.metadata),
        evidence.severity.value if evidence.severity is not None else None,
        (
            evidence.event_time.isoformat()
            if evidence.event_time is not None
            else None
        ),
        evidence.normal_signal,
        evidence.correlation_key,
        _derive_dedupe_key(evidence),
        evidence.created_at.isoformat(),
        evidence.created_by,
    )


def _evidence_from_row(row: tuple[object, ...]) -> EvidenceRef:
    truncation_json = row[14]
    metadata_json = row[15]
    return EvidenceRef(
        evidence_ref_id=row[0],
        incident_id=row[1],
        evidence_kind=EvidenceKind(row[2]),
        agent_run_id=row[3],
        project_id=row[4],
        target_id=row[5],
        service_name=row[6],
        source_ref=row[7],
        source_kind=LogSourceKind(row[8]) if row[8] is not None else None,
        scope=LogScope(row[9]) if row[9] is not None else None,
        cursor=row[10],
        content_redacted=row[11],
        content_sha256=row[12],
        redaction_summary=json.loads(row[13]),
        truncation=(
            TruncationInfo.model_validate_json(truncation_json)
            if truncation_json is not None
            else None
        ),
        metadata=json.loads(metadata_json),
        severity=LogSeverity(row[16]) if row[16] is not None else None,
        event_time=(
            datetime.fromisoformat(row[17]) if row[17] is not None else None
        ),
        normal_signal=row[18],
        correlation_key=row[19],
        created_at=datetime.fromisoformat(row[21]),
        created_by=row[22],
    )


def _legacy_evidence_from_row(row: tuple[object, ...]) -> EvidenceRef:
    """Build an EvidenceRef from a pre-Phase-4 (log-only) row.

    New columns default to None/empty so migrated rows round-trip through the
    unified schema with a derived ``dedupe_key`` identical to what the current
    code computes for the same source identity.
    """
    return EvidenceRef(
        evidence_ref_id=row[0],
        incident_id=row[1],
        evidence_kind=EvidenceKind(row[2]),
        project_id=row[3],
        target_id=row[4],
        service_name=row[5],
        source_ref=row[8],
        source_kind=LogSourceKind(row[6]),
        scope=LogScope(row[7]),
        cursor=row[9],
        content_redacted=row[10],
        content_sha256=row[11],
        redaction_summary=json.loads(row[12]),
        severity=LogSeverity(row[13]),
        event_time=(
            datetime.fromisoformat(row[14]) if row[14] is not None else None
        ),
        normal_signal=row[15],
        correlation_key=row[16],
        created_at=datetime.fromisoformat(row[17]),
        created_by=row[18],
    )


class EvidenceStore:
    """Append-only SQLite persistence for immutable evidence refs."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connection_factory = connection_factory

    def migrate(self) -> None:
        """Create the unified evidence_refs table, upgrading a legacy schema.

        A legacy (log-only) table is renamed aside, the unified schema is
        created, and existing rows are copied over with defaults for the new
        columns and a ``dedupe_key`` derived exactly as the current code would
        derive it.
        """
        with self._connection_factory() as conn:
            legacy = self._table_missing_column(conn, "dedupe_key")
            if legacy:
                conn.execute(
                    "ALTER TABLE evidence_refs RENAME TO evidence_refs_legacy"
                )
                conn.executescript(_SCHEMA_SQL)
                rows = conn.execute(
                    f"""
                    SELECT {", ".join(_LEGACY_COLUMNS)}
                    FROM evidence_refs_legacy
                    """
                ).fetchall()
                for row in rows:
                    evidence = _legacy_evidence_from_row(row)
                    conn.execute(
                        f"""
                        INSERT OR IGNORE INTO evidence_refs
                            ({", ".join(_EVIDENCE_COLUMNS)})
                        VALUES ({", ".join("?" for _ in _EVIDENCE_COLUMNS)})
                        """,
                        _evidence_values(evidence),
                    )
                conn.execute("DROP TABLE IF EXISTS evidence_refs_legacy")
            else:
                conn.executescript(_SCHEMA_SQL)
            conn.commit()

    @staticmethod
    def _table_missing_column(conn: sqlite3.Connection, column: str) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name = 'evidence_refs'"
        ).fetchone()
        if row is None:
            return False
        columns = {
            info[1] for info in conn.execute("PRAGMA table_info(evidence_refs)")
        }
        return column not in columns

    def create(self, evidence: EvidenceRef) -> EvidenceRef:
        """Persist an immutable evidence ref, idempotently.

        The evidence ref id and dedupe key are derived from the source identity
        plus a hash of the redacted content, so re-creating evidence for the
        same source/content yields the same ref and never duplicates rows.
        When the insert is suppressed because the row already exists (by either
        the primary key or the dedupe-key uniqueness), the STORED row wins and
        is returned (the persisted incident_id/created_at are authoritative,
        never the caller's new values).
        """
        values = _evidence_values(evidence)
        dedupe_key = values[20]
        with self._connection_factory() as conn:
            conn.execute(
                f"""
                INSERT OR IGNORE INTO evidence_refs ({", ".join(_EVIDENCE_COLUMNS)})
                VALUES ({", ".join("?" for _ in _EVIDENCE_COLUMNS)})
                """,
                values,
            )
            conn.commit()
            row = conn.execute(
                f"""
                SELECT {", ".join(_EVIDENCE_COLUMNS)}
                FROM evidence_refs
                WHERE evidence_ref_id = ? OR dedupe_key = ?
                """,
                (evidence.evidence_ref_id, dedupe_key),
            ).fetchone()
        if row is None:
            raise KeyError(f"evidence not found: {evidence.evidence_ref_id}")
        return _evidence_from_row(row)

    def create_from_log_record(
        self,
        record: LogRecord,
        incident_id: str,
        created_by: str,
        now: datetime,
    ) -> EvidenceRef:
        """Create an evidence ref for a redacted log record, idempotently."""
        content_sha256 = hashlib.sha256(
            record.message_redacted.encode("utf-8")
        ).hexdigest()
        identity = "|".join(
            (
                record.project_id,
                record.target_id,
                record.service_name,
                record.source_kind.value,
                record.scope.value,
                record.source_ref,
                record.cursor,
            )
        )
        evidence_ref_id = "ev-" + hashlib.sha256(
            f"{identity}|{content_sha256}".encode("utf-8")
        ).hexdigest()[:24]
        evidence = EvidenceRef(
            evidence_ref_id=evidence_ref_id,
            incident_id=incident_id,
            evidence_kind=EvidenceKind.LOG_RECORD,
            project_id=record.project_id,
            target_id=record.target_id,
            service_name=record.service_name,
            source_ref=record.source_ref,
            source_kind=record.source_kind,
            scope=record.scope,
            cursor=record.cursor,
            content_redacted=record.message_redacted,
            content_sha256=content_sha256,
            redaction_summary=record.redaction_summary,
            severity=record.severity,
            event_time=record.event_time,
            normal_signal=record.normal_signal,
            correlation_key=record.correlation_key,
            created_at=now,
            created_by=created_by,
        )
        return self.create(evidence)

    def get(self, evidence_ref_id: str) -> EvidenceRef:
        """Return the evidence ref with the given id, or raise KeyError."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_EVIDENCE_COLUMNS)}
                FROM evidence_refs
                WHERE evidence_ref_id = ?
                """,
                (evidence_ref_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"evidence not found: {evidence_ref_id}")
        return _evidence_from_row(row)

    def query(
        self,
        *,
        incident_id: str | None = None,
        agent_run_id: str | None = None,
        evidence_kind: EvidenceKind | None = None,
        limit: int = 100,
    ) -> tuple[EvidenceRef, ...]:
        """Return evidence refs filtered by incident, run and/or kind."""
        if not (1 <= limit <= 1000):
            raise ValueError("limit must be between 1 and 1000")
        clauses: list[str] = []
        params: list[object] = []
        if incident_id is not None:
            clauses.append("incident_id = ?")
            params.append(incident_id)
        if agent_run_id is not None:
            clauses.append("agent_run_id = ?")
            params.append(agent_run_id)
        if evidence_kind is not None:
            clauses.append("evidence_kind = ?")
            params.append(evidence_kind.value)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_EVIDENCE_COLUMNS)}
                FROM evidence_refs {where_sql}
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return tuple(_evidence_from_row(row) for row in rows)

    def list_for_incident(
        self, incident_id: str, limit: int = 100
    ) -> tuple[EvidenceRef, ...]:
        """Return evidence refs for an incident, oldest first."""
        return self.query(incident_id=incident_id, limit=limit)

    def list_for_agent_run(
        self,
        agent_run_id: str,
        *,
        incident_id: str | None = None,
        limit: int = 100,
    ) -> tuple[EvidenceRef, ...]:
        """Return evidence refs for an agent run, optionally within an incident."""
        return self.query(
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            limit=limit,
        )

    def list_by_kind(
        self,
        evidence_kind: EvidenceKind,
        *,
        incident_id: str | None = None,
        agent_run_id: str | None = None,
        limit: int = 100,
    ) -> tuple[EvidenceRef, ...]:
        """Return evidence refs of a kind, optionally filtered by incident/run."""
        return self.query(
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            evidence_kind=evidence_kind,
            limit=limit,
        )
