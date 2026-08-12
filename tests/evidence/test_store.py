import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.evidence.types import EvidenceKind, EvidenceRef
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


def test_create_evidence_second_incident_returns_stored_row(tmp_path: Path) -> None:
    """A suppressed idempotent insert must return the persisted row.

    The UNIQUE key and ref-id derivation exclude incident_id, so citing the
    same source/cursor/content under a second incident does not insert. The
    returned ref must therefore reflect the STORED row (inc-1), never the
    caller's new incident_id.
    """
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
        incident_id="inc-2",
        created_by="bob",
        now=datetime(2026, 8, 12, 1, 0, tzinfo=UTC),
    )

    assert second.evidence_ref_id == first.evidence_ref_id
    assert second.incident_id == "inc-1"
    assert store.list_for_incident("inc-1", limit=10) == (first,)
    assert store.list_for_incident("inc-2", limit=10) == ()


def test_evidence_schema_has_no_raw_content_column(tmp_path: Path) -> None:
    store = EvidenceStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    with sqlite3.connect(tmp_path / "runtime.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence_refs)")}

    assert "content_raw" not in columns
    assert "content_redacted" in columns


def test_unified_schema_has_typed_columns(tmp_path: Path) -> None:
    store = EvidenceStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    with sqlite3.connect(tmp_path / "runtime.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence_refs)")}

    assert "content_raw" not in columns
    assert "dedupe_key" in columns
    assert "agent_run_id" in columns
    assert "metadata_json" in columns
    assert "truncation_json" in columns


def _make_typed_ref(
    *,
    evidence_ref_id: str,
    incident_id: str,
    agent_run_id: str | None,
    content: str,
    kind: EvidenceKind = EvidenceKind.COMMAND_OUTPUT,
    metadata: dict[str, str] | None = None,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_ref_id=evidence_ref_id,
        incident_id=incident_id,
        evidence_kind=kind,
        agent_run_id=agent_run_id,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_ref="host:dev-a",
        content_redacted=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        redaction_summary={"token": 1},
        truncation=None,
        metadata=metadata or {},
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        created_by="alice",
    )


def test_generic_create_persists_typed_evidence(tmp_path: Path) -> None:
    store = EvidenceStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    ref = _make_typed_ref(
        evidence_ref_id="ev-cmd-1",
        incident_id="inc-1",
        agent_run_id="run-1",
        content="mysql restarted token=[REDACTED_TOKEN]",
        metadata={"command": "systemctl restart mysql", "exit_code": "0"},
    )

    stored = store.create(ref)

    assert stored.evidence_ref_id == "ev-cmd-1"
    assert stored.evidence_kind == EvidenceKind.COMMAND_OUTPUT
    assert stored.agent_run_id == "run-1"
    assert stored.metadata == {
        "command": "systemctl restart mysql",
        "exit_code": "0",
    }
    assert stored.truncation is None
    # Log-specific identity must be NULL for non-log evidence.
    assert stored.source_kind is None
    assert stored.scope is None
    assert stored.cursor is None
    assert stored.severity is None
    assert stored.event_time is None
    assert stored.normal_signal is None
    assert stored.correlation_key is None


def test_query_filters_by_kind_and_run(tmp_path: Path) -> None:
    store = EvidenceStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    store.create(
        _make_typed_ref(
            evidence_ref_id="ev-log",
            incident_id="inc-1",
            agent_run_id=None,
            content="ERROR token=[REDACTED_TOKEN]",
            kind=EvidenceKind.LOG_RECORD,
        )
    )
    store.create(
        _make_typed_ref(
            evidence_ref_id="ev-cmd-a",
            incident_id="inc-1",
            agent_run_id="run-1",
            content="restart a token=[REDACTED_TOKEN]",
        )
    )
    store.create(
        _make_typed_ref(
            evidence_ref_id="ev-cmd-b",
            incident_id="inc-1",
            agent_run_id="run-2",
            content="restart b token=[REDACTED_TOKEN]",
        )
    )
    store.create(
        _make_typed_ref(
            evidence_ref_id="ev-cmd-c",
            incident_id="inc-2",
            agent_run_id="run-1",
            content="restart c token=[REDACTED_TOKEN]",
        )
    )

    by_kind = store.query(incident_id="inc-1", evidence_kind=EvidenceKind.COMMAND_OUTPUT)
    assert {r.evidence_ref_id for r in by_kind} == {"ev-cmd-a", "ev-cmd-b"}
    assert store.list_by_kind(EvidenceKind.COMMAND_OUTPUT, incident_id="inc-1") == by_kind

    run_scoped = store.query(incident_id="inc-1", agent_run_id="run-1")
    assert {r.evidence_ref_id for r in run_scoped} == {"ev-cmd-a"}
    assert store.list_for_agent_run("run-1", incident_id="inc-1") == run_scoped

    # Across incidents the same run collects both refs.
    assert {r.evidence_ref_id for r in store.list_for_agent_run("run-1")} == {
        "ev-cmd-a",
        "ev-cmd-c",
    }
    assert {r.evidence_ref_id for r in store.list_for_incident("inc-1")} == {
        "ev-log",
        "ev-cmd-a",
        "ev-cmd-b",
    }


_LEGACY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
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
    created_by TEXT NOT NULL
);
"""

_LONG_CORRELATION_KEY = "trace:" + "x" * 3000


def _install_legacy_table(
    conn: sqlite3.Connection, table: str = "evidence_refs"
) -> None:
    """Create a pre-Phase-4 log-only table and seed one row."""
    conn.executescript(_LEGACY_SCHEMA_SQL.format(table=table))
    record = make_log_record()
    conn.execute(
        f"""
        INSERT INTO {table} (
            evidence_ref_id, incident_id, evidence_kind, project_id, target_id,
            service_name, source_kind, scope, source_ref, cursor,
            content_redacted, content_sha256, redaction_summary_json,
            severity, event_time, normal_signal, correlation_key,
            created_at, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ev-legacy-1",
            "inc-1",
            "log_record",
            record.project_id,
            record.target_id,
            record.service_name,
            record.source_kind.value,
            record.scope.value,
            record.source_ref,
            record.cursor,
            record.message_redacted,
            hashlib.sha256(record.message_redacted.encode("utf-8")).hexdigest(),
            '{"token": 1}',
            record.severity.value,
            None,
            None,
            _LONG_CORRELATION_KEY,
            "2026-08-12T10:00:00+00:00",
            "alice",
        ),
    )


def test_migration_upgrades_legacy_schema_preserving_rows(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    legacy = sqlite3.connect(db)
    _install_legacy_table(legacy)
    legacy.commit()
    legacy.close()

    store = EvidenceStore(lambda: sqlite3.connect(db))
    store.migrate()

    record = make_log_record()
    ref = store.get("ev-legacy-1")
    assert ref.incident_id == "inc-1"
    assert ref.evidence_kind == EvidenceKind.LOG_RECORD
    assert ref.content_redacted == record.message_redacted
    assert ref.redaction_summary == {"token": 1}
    # A legacy row with an arbitrarily long derived correlation_key must
    # survive the migration (no silent length regression).
    assert ref.correlation_key == _LONG_CORRELATION_KEY
    # New columns defaulted for migrated rows.
    assert ref.agent_run_id is None
    assert ref.metadata == {}
    assert ref.truncation is None
    assert store.list_for_incident("inc-1", limit=10) == (ref,)

    # Re-creating the same record after migration dedupes on the derived
    # dedupe_key (the legacy id differs from the derived id), returning the
    # migrated row and never inserting a duplicate.
    recreated = store.create_from_log_record(
        record,
        incident_id="inc-1",
        created_by="bob",
        now=datetime(2026, 8, 12, 11, 0, tzinfo=UTC),
    )
    assert recreated.evidence_ref_id == "ev-legacy-1"
    assert store.list_for_incident("inc-1", limit=10) == (recreated,)


def test_log_evidence_accepts_long_correlation_key(tmp_path: Path) -> None:
    store = EvidenceStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    record = make_log_record().model_copy(
        update={
            "correlation_key": "x" * 2000,
            "normal_signal": "y" * 2000,
        }
    )

    evidence = store.create_from_log_record(
        record,
        incident_id="inc-1",
        created_by="alice",
        now=datetime(2026, 8, 12, 10, 1, tzinfo=UTC),
    )

    assert evidence.correlation_key == "x" * 2000
    assert evidence.normal_signal == "y" * 2000
    stored = store.get(evidence.evidence_ref_id)
    assert stored.correlation_key == "x" * 2000
    assert stored.normal_signal == "y" * 2000


def test_migration_failure_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import incidentlens_control_plane.evidence.store as store_module

    db = tmp_path / "runtime.db"
    legacy = sqlite3.connect(db)
    _install_legacy_table(legacy)
    legacy.commit()
    legacy.close()

    real_legacy_from_row = store_module._legacy_evidence_from_row
    calls = {"n": 0}

    def flaky_legacy_from_row(row: tuple[object, ...]):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated migration failure")
        return real_legacy_from_row(row)

    monkeypatch.setattr(
        store_module, "_legacy_evidence_from_row", flaky_legacy_from_row
    )

    store = EvidenceStore(lambda: sqlite3.connect(db))
    with pytest.raises(RuntimeError):
        store.migrate()

    # The failed migration rolled the whole upgrade back: evidence_refs is
    # still the legacy table, no stranded legacy table remains, and the row is
    # untouched.
    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "evidence_refs_legacy" not in tables
        columns = {
            info[1] for info in conn.execute("PRAGMA table_info(evidence_refs)")
        }
        assert "dedupe_key" not in columns
        assert conn.execute("SELECT count(*) FROM evidence_refs").fetchone()[0] == 1

    # A retry migrates cleanly and preserves the row.
    store.migrate()
    ref = store.get("ev-legacy-1")
    assert ref.incident_id == "inc-1"
    assert ref.correlation_key == _LONG_CORRELATION_KEY


def test_migration_resumes_from_leftover_legacy_table(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    store = EvidenceStore(lambda: sqlite3.connect(db))
    store.migrate()
    store.create(
        _make_typed_ref(
            evidence_ref_id="ev-new",
            incident_id="inc-1",
            agent_run_id="run-1",
            content="new token=[REDACTED_TOKEN]",
        )
    )

    # Simulate an older interrupted run that left rows stranded in the renamed
    # table after evidence_refs had already been rebuilt.
    conn = sqlite3.connect(db)
    _install_legacy_table(conn, table="evidence_refs_legacy")
    conn.commit()
    conn.close()

    store.migrate()

    # Both the pre-existing unified row and the merged legacy row are present.
    assert store.get("ev-new").incident_id == "inc-1"
    ref = store.get("ev-legacy-1")
    assert ref.incident_id == "inc-1"
    assert ref.correlation_key == _LONG_CORRELATION_KEY
    assert {r.evidence_ref_id for r in store.list_for_incident("inc-1")} == {
        "ev-new",
        "ev-legacy-1",
    }
    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "evidence_refs_legacy" not in tables


def test_migrate_is_idempotent_on_unified_schema(tmp_path: Path) -> None:
    store = EvidenceStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    store.create(
        _make_typed_ref(
            evidence_ref_id="ev-cmd-1",
            incident_id="inc-1",
            agent_run_id="run-1",
            content="ok token=[REDACTED_TOKEN]",
        )
    )
    store.migrate()
    assert store.get("ev-cmd-1").incident_id == "inc-1"

