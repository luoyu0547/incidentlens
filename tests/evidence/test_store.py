import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.logs.types import LogRecord, LogScope, LogSeverity, LogSourceKind


def make_log_record() -> LogRecord:
    return LogRecord(
        log_id="log-1",
        subscription_id=None,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        cursor="offset:1",
        dedupe_key="dedupe-1",
        observed_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        event_time=None,
        severity=LogSeverity.ERROR,
        message_redacted="ERROR token=[REDACTED_TOKEN]",
        redaction_summary={"token": 1},
        normal_signal=None,
        correlation_key="trace:abc",
        evidence_ref_id=None,
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


def test_create_evidence_hashes_redacted_content(tmp_path: Path) -> None:
    store = EvidenceStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    record = make_log_record()

    evidence = store.create_from_log_record(
        record,
        incident_id="inc-1",
        created_by="alice",
        now=datetime(2026, 8, 12, 10, 1, tzinfo=UTC),
    )

    assert evidence.content_redacted == record.message_redacted
    assert evidence.content_sha256 == hashlib.sha256(record.message_redacted.encode()).hexdigest()
    assert "abc123" not in evidence.model_dump_json()


def test_create_evidence_is_idempotent_for_same_source_cursor_hash(tmp_path: Path) -> None:
    store = EvidenceStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    record = make_log_record()

    first = store.create_from_log_record(
        record,
        incident_id="inc-1",
        created_by="alice",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    second = store.create_from_log_record(
        record,
        incident_id="inc-1",
        created_by="alice",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert second.evidence_ref_id == first.evidence_ref_id
    assert store.list_for_incident("inc-1", limit=10) == (first,)


def test_evidence_schema_has_no_raw_content_column(tmp_path: Path) -> None:
    store = EvidenceStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    with sqlite3.connect(tmp_path / "runtime.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence_refs)")}

    assert "content_raw" not in columns
    assert "content_redacted" in columns
