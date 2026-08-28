import sqlite3
from datetime import UTC, datetime

from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.approvals.types import (
    ApprovalDownstreamStatus,
    ApprovalStatus,
)

INTENT = {
    "kind": "docker.restart",
    "target_id": "dev-a",
    "container": "payments-api-1",
    "argv": ["docker", "restart", "payments-api-1"],
}


def test_store_persists_approval_request(tmp_path):
    """Approval request survives across store instances."""
    db_path = tmp_path / "approvals.db"
    store1 = ApprovalStore(lambda: __import__("sqlite3").connect(db_path))
    store1.migrate()

    record = store1.create_request(
        approval_id="apr-001",
        intent_sha256="abc123",
        intent=INTENT,
        intent_summary="docker.restart payments-api-1",
        now=datetime(2026, 8, 10, tzinfo=UTC),
    )
    assert record.status == ApprovalStatus.PENDING

    store2 = ApprovalStore(lambda: __import__("sqlite3").connect(db_path))
    store2.migrate()

    loaded = store2.get("apr-001")
    assert loaded is not None
    assert loaded.approval_id == "apr-001"
    assert loaded.status == ApprovalStatus.PENDING


def test_store_persists_approved_state(tmp_path):
    """Approved status survives across store instances."""
    db_path = tmp_path / "approvals.db"
    store1 = ApprovalStore(lambda: __import__("sqlite3").connect(db_path))
    store1.migrate()

    now = datetime(2026, 8, 10, tzinfo=UTC)
    store1.create_request(
        approval_id="apr-002",
        intent_sha256="def456",
        intent=INTENT,
        intent_summary="docker.restart payments-api-1",
        now=now,
    )
    store1.approve("apr-002", now=now)

    store2 = ApprovalStore(lambda: __import__("sqlite3").connect(db_path))
    store2.migrate()

    loaded = store2.get("apr-002")
    assert loaded.status == ApprovalStatus.APPROVED


def test_store_persists_rejected_state(tmp_path):
    """Rejected status survives across store instances."""
    db_path = tmp_path / "approvals.db"
    store1 = ApprovalStore(lambda: __import__("sqlite3").connect(db_path))
    store1.migrate()

    now = datetime(2026, 8, 10, tzinfo=UTC)
    store1.create_request(
        approval_id="apr-003",
        intent_sha256="ghi789",
        intent=INTENT,
        intent_summary="docker.restart payments-api-1",
        now=now,
    )
    store1.reject("apr-003", now=now)

    store2 = ApprovalStore(lambda: __import__("sqlite3").connect(db_path))
    store2.migrate()

    loaded = store2.get("apr-003")
    assert loaded.status == ApprovalStatus.REJECTED


def test_store_persists_consumed_state(tmp_path):
    """Consumed status survives across store instances."""
    db_path = tmp_path / "approvals.db"
    store1 = ApprovalStore(lambda: __import__("sqlite3").connect(db_path))
    store1.migrate()

    now = datetime(2026, 8, 10, tzinfo=UTC)
    store1.create_request(
        approval_id="apr-004",
        intent_sha256="jkl012",
        intent=INTENT,
        intent_summary="docker.restart payments-api-1",
        now=now,
    )
    store1.approve("apr-004", now=now)
    store1.consume("apr-004", now=now)

    store2 = ApprovalStore(lambda: __import__("sqlite3").connect(db_path))
    store2.migrate()

    loaded = store2.get("apr-004")
    assert loaded.status == ApprovalStatus.CONSUMED


def test_migrate_backfills_old_schema_linked_rows_with_derived_values(tmp_path) -> None:
    db_path = tmp_path / "approvals.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE approvals (
                approval_id TEXT PRIMARY KEY,
                intent_sha256 TEXT NOT NULL,
                intent_json TEXT NOT NULL,
                intent_summary TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                decided_at TEXT,
                consumed_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO approvals (
                approval_id, intent_sha256, intent_json, intent_summary, status,
                created_at, expires_at, decided_at, consumed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "apr-legacy",
                "z" * 64,
                (
                    '{"agent_run_id":"run-legacy","changeset_id":"chs-legacy",'
                    '"investigation_id":"inv-legacy","kind":"registry_update",'
                    '"preview":{"preview":"Safe summary"},"proposal_id":"prop-legacy",'
                    '"risk":"backup_required","service":"payment-api",'
                    '"target_id":"dev-a"}'
                ),
                "legacy approval",
                "approved",
                datetime(2026, 8, 10, tzinfo=UTC).isoformat(),
                datetime(2026, 8, 10, 0, 15, tzinfo=UTC).isoformat(),
                datetime(2026, 8, 10, 0, 5, tzinfo=UTC).isoformat(),
                None,
            ),
        )
        conn.commit()

    store = ApprovalStore(lambda: sqlite3.connect(db_path))
    store.migrate()

    loaded = store.get("apr-legacy")
    assert loaded is not None
    assert loaded.target_id == "dev-a"
    assert loaded.service == "payment-api"
    assert loaded.investigation_id == "inv-legacy"
    assert loaded.agent_run_id == "run-legacy"
    assert loaded.changeset_id == "chs-legacy"
    assert loaded.proposal_id == "prop-legacy"
    assert loaded.risk == "backup_required"
    assert loaded.preview == {"preview": "Safe summary"}
    assert loaded.downstream_status is ApprovalDownstreamStatus.PENDING


def test_list_pages_through_more_than_ten_thousand_records(tmp_path) -> None:
    db_path = tmp_path / "approvals.db"
    store = ApprovalStore(lambda: sqlite3.connect(db_path))
    store.migrate()

    created_at = datetime(2026, 8, 10, tzinfo=UTC).isoformat()
    expires_at = datetime(2026, 8, 10, 0, 15, tzinfo=UTC).isoformat()
    rows = [
        (
            f"apr-{index:05d}",
            f"{index:064x}"[-64:],
            (
                '{"kind":"docker.restart","target_id":"dev-a",'
                '"service":"payments-api"}'
            ),
            f"approval {index}",
            "pending",
            "dev-a",
            "payments-api",
            None,
            None,
            None,
            None,
            None,
            None,
            "approval_required",
            "{}",
            created_at,
            expires_at,
            None,
            None,
            None,
            None,
            "not_applicable",
            None,
            None,
        )
        for index in range(10_050)
    ]
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO approvals (
                approval_id, intent_sha256, intent_json, intent_summary, status,
                target_id, service, session_id, investigation_id, agent_run_id,
                tool_call_id, changeset_id, proposal_id, risk, preview_json,
                created_at, expires_at, decided_at, consumed_at, decision_actor,
                decision_reason, downstream_status, downstream_error_code,
                downstream_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

    loaded = store.list(status=ApprovalStatus.PENDING)

    assert len(loaded) == 10_050
    assert loaded[0].approval_id == "apr-00000"
    assert loaded[-1].approval_id == "apr-10049"
