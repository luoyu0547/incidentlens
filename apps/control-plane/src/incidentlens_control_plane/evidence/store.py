"""Append-only Evidence Store over redacted log content.

The Evidence Store is immutable: evidence refs are created once and never
updated or deleted. Content hashes are computed exclusively over the already
redacted message so raw log text never reaches this store.
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
)

_EVIDENCE_COLUMNS = (
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


def _evidence_from_row(row: tuple[object, ...]) -> EvidenceRef:
    return EvidenceRef(
        evidence_ref_id=row[0],
        incident_id=row[1],
        evidence_kind=EvidenceKind(row[2]),
        project_id=row[3],
        target_id=row[4],
        service_name=row[5],
        source_kind=LogSourceKind(row[6]),
        scope=LogScope(row[7]),
        source_ref=row[8],
        cursor=row[9],
        content_redacted=row[10],
        content_sha256=row[11],
        redaction_summary=json.loads(row[12]),
        severity=LogSeverity(row[13]),
        event_time=datetime.fromisoformat(row[14]) if row[14] is not None else None,
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
        """Create the evidence_refs table if it doesn't exist."""
        with self._connection_factory() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS evidence_refs (
                    evidence_ref_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    evidence_kind TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    content_redacted TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    redaction_summary_json TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    event_time TEXT,
                    normal_signal TEXT,
                    correlation_key TEXT,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    UNIQUE(
                        project_id,
                        target_id,
                        service_name,
                        source_kind,
                        scope,
                        source_ref,
                        cursor,
                        content_sha256
                    )
                );
                """
            )
            conn.commit()

    def create_from_log_record(
        self,
        record: LogRecord,
        incident_id: str,
        created_by: str,
        now: datetime,
    ) -> EvidenceRef:
        """Create an evidence ref for a redacted log record, idempotently.

        The evidence ref id is derived from the source identity plus a hash of
        the redacted content, so re-creating evidence for the same
        source/cursor/content yields the same ref and never duplicates rows.
        When the insert is suppressed because the row already exists, the
        STORED row wins and is returned (the persisted incident_id/created_at
        are authoritative, never the caller's new values).
        """
        content_sha256 = hashlib.sha256(
            record.message_redacted.encode("utf-8")
        ).hexdigest()
        evidence_ref_id = "ev-" + hashlib.sha256(
            f"{record.project_id}|{record.target_id}|{record.service_name}|"
            f"{record.source_kind.value}|{record.scope.value}|{record.source_ref}|"
            f"{record.cursor}|{content_sha256}".encode("utf-8")
        ).hexdigest()[:24]
        evidence = EvidenceRef(
            evidence_ref_id=evidence_ref_id,
            incident_id=incident_id,
            evidence_kind=EvidenceKind.LOG_RECORD,
            project_id=record.project_id,
            target_id=record.target_id,
            service_name=record.service_name,
            source_kind=record.source_kind,
            scope=record.scope,
            source_ref=record.source_ref,
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
        with self._connection_factory() as conn:
            conn.execute(
                f"""
                INSERT OR IGNORE INTO evidence_refs ({", ".join(_EVIDENCE_COLUMNS)})
                VALUES ({", ".join("?" for _ in _EVIDENCE_COLUMNS)})
                """,
                (
                    evidence.evidence_ref_id,
                    evidence.incident_id,
                    evidence.evidence_kind.value,
                    evidence.project_id,
                    evidence.target_id,
                    evidence.service_name,
                    evidence.source_kind.value,
                    evidence.scope.value,
                    evidence.source_ref,
                    evidence.cursor,
                    evidence.content_redacted,
                    evidence.content_sha256,
                    json.dumps(evidence.redaction_summary),
                    evidence.severity.value,
                    (
                        evidence.event_time.isoformat()
                        if evidence.event_time is not None
                        else None
                    ),
                    evidence.normal_signal,
                    evidence.correlation_key,
                    evidence.created_at.isoformat(),
                    evidence.created_by,
                ),
            )
            conn.commit()
        return self.get(evidence_ref_id)

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

    def list_for_incident(
        self, incident_id: str, limit: int = 100
    ) -> tuple[EvidenceRef, ...]:
        """Return evidence refs for an incident, oldest first."""
        if not (1 <= limit <= 1000):
            raise ValueError("limit must be between 1 and 1000")
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_EVIDENCE_COLUMNS)}
                FROM evidence_refs
                WHERE incident_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (incident_id, limit),
            ).fetchall()
        return tuple(_evidence_from_row(row) for row in rows)
