from datetime import UTC, datetime

from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.approvals.types import ApprovalDownstreamStatus

INTENT = {
    "kind": "registry_update",
    "target_id": "dev-a",
    "service": "payment-api",
}


def test_unlinked_requests_default_to_not_applicable(tmp_path) -> None:
    db_path = tmp_path / "approvals.db"
    store = ApprovalStore(lambda: __import__("sqlite3").connect(db_path))
    store.migrate()

    record = store.create_request(
        approval_id="apr-001",
        intent_sha256="a" * 64,
        intent=INTENT,
        intent_summary="registry update on dev-a",
        now=datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert record.downstream_status is ApprovalDownstreamStatus.NOT_APPLICABLE


def test_linked_requests_start_pending_and_mark_processed(tmp_path) -> None:
    db_path = tmp_path / "approvals.db"
    store = ApprovalStore(lambda: __import__("sqlite3").connect(db_path))
    store.migrate()

    record = store.create_request(
        approval_id="apr-002",
        intent_sha256="b" * 64,
        intent=INTENT,
        intent_summary="registry update on dev-a",
        now=datetime(2026, 8, 20, tzinfo=UTC),
        target_id="dev-a",
        service="payment-api",
        investigation_id="inv-1",
        proposal_id="prop-1",
    )
    assert record.downstream_status is ApprovalDownstreamStatus.PENDING

    processed = store.mark_downstream(
        "apr-002",
        ApprovalDownstreamStatus.PROCESSED,
        now=datetime(2026, 8, 20, 12, 5, tzinfo=UTC),
    )
    assert processed.downstream_status is ApprovalDownstreamStatus.PROCESSED
    assert processed.downstream_updated_at is not None


def test_failed_downstream_persists_bounded_error_code(tmp_path) -> None:
    db_path = tmp_path / "approvals.db"
    store = ApprovalStore(lambda: __import__("sqlite3").connect(db_path))
    store.migrate()
    store.create_request(
        approval_id="apr-003",
        intent_sha256="c" * 64,
        intent=INTENT,
        intent_summary="registry update on dev-a",
        now=datetime(2026, 8, 20, tzinfo=UTC),
        target_id="dev-a",
        service="payment-api",
        investigation_id="inv-2",
    )

    failed = store.mark_downstream(
        "apr-003",
        ApprovalDownstreamStatus.FAILED,
        error_code="internal_error",
        now=datetime(2026, 8, 20, 12, 6, tzinfo=UTC),
    )
    assert failed.downstream_status is ApprovalDownstreamStatus.FAILED
    assert failed.downstream_error_code == "internal_error"
