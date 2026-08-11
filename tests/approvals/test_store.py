from datetime import UTC, datetime

from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.approvals.types import ApprovalStatus

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
