"""Store-level tests for durable operations and their attempts."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from incidentlens_control_plane.investigation.state_machine import IllegalTransition
from incidentlens_control_plane.operations.store import (
    ConcurrentOperationUpdate,
    OperationAlreadyExists,
    OperationNotClaimable,
    OperationNotFound,
    OperationStore,
)
from incidentlens_control_plane.operations.types import (
    Operation,
    OperationKind,
    OperationStatus,
)

NOW = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> OperationStore:
    return OperationStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))


def _op(
    *,
    operation_id: str = "op-1",
    status: OperationStatus = OperationStatus.QUEUED,
    target_id: str = "tgt-a",
    created_by: str = "alice",
    session_id: str | None = None,
    investigation_id: str | None = None,
    request_payload: str | None = None,
    progress_summary: str = "checking",
    created_at: datetime = NOW,
    updated_at: datetime = NOW,
) -> Operation:
    return Operation(
        operation_id=operation_id,
        kind=OperationKind.TARGET_TEST,
        status=status,
        target_id=target_id,
        created_by=created_by,
        session_id=session_id,
        investigation_id=investigation_id,
        request_payload=request_payload,
        progress_summary=progress_summary,
        created_at=created_at,
        updated_at=updated_at,
    )


def test_create_and_get_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.migrate()
    store.create(_op())

    stored = store.get("op-1")
    assert stored.operation_id == "op-1"
    assert stored.kind == OperationKind.TARGET_TEST
    assert stored.status == OperationStatus.QUEUED
    assert stored.target_id == "tgt-a"
    assert stored.created_by == "alice"
    assert stored.progress_summary == "checking"
    assert stored.claimed_at is None
    assert stored.finished_at is None
    assert stored.created_at == NOW


def test_create_duplicate_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.migrate()
    store.create(_op())
    with pytest.raises(OperationAlreadyExists):
        store.create(_op())


def test_get_missing_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.migrate()
    with pytest.raises(OperationNotFound):
        store.get("op-missing")


def test_claim_single_worker_moves_to_running_and_records_attempt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.migrate()
    store.create(_op())

    claimed = store.claim(
        "op-1", claim_token="worker-1", now=datetime(2026, 8, 24, 10, 1, tzinfo=UTC)
    )
    assert claimed.status == OperationStatus.RUNNING
    assert claimed.claim_token == "worker-1"
    assert claimed.claimed_at == datetime(2026, 8, 24, 10, 1, tzinfo=UTC)

    attempts = store.list_attempts("op-1")
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.operation_id == "op-1"
    assert attempt.status == "running"
    assert attempt.claimed_by == "worker-1"
    assert attempt.finished_at is None


def test_claim_is_atomic_one_worker_wins(tmp_path: Path) -> None:
    """A second claim of the same operation must fail — no claim race."""
    store = _store(tmp_path)
    store.migrate()
    store.create(_op())

    first = store.claim("op-1", claim_token="worker-1", now=NOW)
    assert first.status == OperationStatus.RUNNING

    with pytest.raises(OperationNotClaimable):
        store.claim(
            "op-1", claim_token="worker-2", now=datetime(2026, 8, 24, 10, 2, tzinfo=UTC)
        )
    # The winning attempt is the only one recorded.
    attempts = store.list_attempts("op-1")
    assert len(attempts) == 1
    assert attempts[0].claimed_by == "worker-1"


def test_claim_missing_operation_raises_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.migrate()
    with pytest.raises(OperationNotFound):
        store.claim("op-missing", claim_token="worker-1", now=NOW)


def test_transition_rejects_illegal_move(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.migrate()
    store.create(_op())
    op = store.get("op-1")
    # queued -> succeeded is illegal
    with pytest.raises(IllegalTransition):
        store.transition(
            op, OperationStatus.SUCCEEDED, now=datetime(2026, 8, 24, 10, 1, tzinfo=UTC)
        )


def test_transition_is_conditional_on_current_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.migrate()
    store.create(_op())
    store.claim("op-1", claim_token="worker-1", now=NOW)
    stale = store.get("op-1")
    # Advance the operation behind the caller's back.
    store.transition(
        stale,
        OperationStatus.SUCCEEDED,
        now=datetime(2026, 8, 24, 10, 2, tzinfo=UTC),
    )
    # Re-applying a transition from the stale snapshot is a concurrent mutation.
    with pytest.raises(ConcurrentOperationUpdate):
        store.transition(
            stale,
            OperationStatus.FAILED,
            now=datetime(2026, 8, 24, 10, 3, tzinfo=UTC),
        )


def test_terminal_transition_sets_finished_at_and_finalizes_attempt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.migrate()
    store.create(_op())
    store.claim("op-1", claim_token="worker-1", now=NOW)
    running = store.get("op-1")
    finished_now = datetime(2026, 8, 24, 10, 5, tzinfo=UTC)

    succeeded = store.transition(running, OperationStatus.SUCCEEDED, now=finished_now)

    assert succeeded.status == OperationStatus.SUCCEEDED
    assert succeeded.finished_at == finished_now
    attempts = store.list_attempts("op-1")
    assert len(attempts) == 1
    assert attempts[0].status == "succeeded"
    assert attempts[0].finished_at == finished_now


def test_cancel_requested_keeps_finished_at_null(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.migrate()
    store.create(_op())
    store.claim("op-1", claim_token="worker-1", now=NOW)
    running = store.get("op-1")

    requested = store.transition(
        running,
        OperationStatus.CANCEL_REQUESTED,
        now=datetime(2026, 8, 24, 10, 2, tzinfo=UTC),
    )
    assert requested.status == OperationStatus.CANCEL_REQUESTED
    assert requested.finished_at is None


def test_list_queued_orders_by_created_at_and_limits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.migrate()
    store.create(
        _op(
            operation_id="op-2",
            created_at=datetime(2026, 8, 24, 10, 2, tzinfo=UTC),
            updated_at=datetime(2026, 8, 24, 10, 2, tzinfo=UTC),
        )
    )
    store.create(
        _op(
            operation_id="op-1",
            created_at=datetime(2026, 8, 24, 10, 1, tzinfo=UTC),
            updated_at=datetime(2026, 8, 24, 10, 1, tzinfo=UTC),
        )
    )
    store.create(_op(operation_id="op-running", status=OperationStatus.RUNNING))

    queued = store.list_queued()
    assert [op.operation_id for op in queued] == ["op-1", "op-2"]
    assert store.list_queued(limit=1) == (queued[0],)


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.migrate()
    store.create(_op())
    store.migrate()
    assert store.get("op-1").operation_id == "op-1"


def test_schema_has_expected_tables_and_indexes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.migrate()
    with sqlite3.connect(tmp_path / "runtime.db") as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        index_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    assert "operations" in tables
    assert "operation_attempts" in tables
    assert "idx_operations_claim" in index_names
    assert "idx_operations_session" in index_names
    assert "idx_operations_investigation" in index_names
