"""State-machine and cancellation-semantics tests for durable operations."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEventType
from incidentlens_control_plane.investigation.state_machine import IllegalTransition
from incidentlens_control_plane.operations.events import OperationEventPublisher
from incidentlens_control_plane.operations.service import OperationService
from incidentlens_control_plane.operations.state_machine import (
    OPERATION_STATE_MACHINE,
    OPERATION_TERMINAL,
    OPERATION_TRANSITIONS,
    OperationNotCancellable,
)
from incidentlens_control_plane.operations.store import OperationStore
from incidentlens_control_plane.operations.types import OperationKind, OperationStatus

NOW = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


def _stack(tmp_path: Path) -> tuple[OperationService, RuntimeEventStore]:
    def connection() -> sqlite3.Connection:
        return sqlite3.connect(tmp_path / "runtime.db")

    events = RuntimeEventStore(connection)
    events.migrate()
    store = OperationStore(connection)
    store.migrate()
    service = OperationService(
        store=store,
        publisher=OperationEventPublisher(events, RuntimeEventBroker()),
    )
    return service, events


def _create_op(
    service: OperationService,
    *,
    created_by: str = "alice",
    target_id: str = "tgt-a",
    progress_summary: str | None = "checking",
    request_payload: str | None = None,
) -> object:
    return service.create_operation(
        kind=OperationKind.TARGET_TEST,
        target_id=target_id,
        created_by=created_by,
        progress_summary=progress_summary,
        request_payload=request_payload,
        now=NOW,
    )


# -- transition table ----------------------------------------------------------


def test_queued_transition_targets() -> None:
    assert OPERATION_TRANSITIONS[OperationStatus.QUEUED] == frozenset(
        {
            OperationStatus.RUNNING,
            OperationStatus.CANCEL_REQUESTED,
            OperationStatus.CANCELLED,
        }
    )


def test_running_transition_targets() -> None:
    assert OPERATION_TRANSITIONS[OperationStatus.RUNNING] == frozenset(
        {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.CANCEL_REQUESTED,
            OperationStatus.UNCERTAIN,
        }
    )


def test_cancel_requested_transition_targets() -> None:
    assert OPERATION_TRANSITIONS[OperationStatus.CANCEL_REQUESTED] == frozenset(
        {
            OperationStatus.CANCELLED,
            OperationStatus.FAILED,
            OperationStatus.UNCERTAIN,
        }
    )


def test_terminal_states_are_absorbing() -> None:
    assert OPERATION_TERMINAL == frozenset(
        {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
            OperationStatus.UNCERTAIN,
        }
    )
    for terminal in OPERATION_TERMINAL:
        assert OPERATION_STATE_MACHINE.transitions(terminal) == frozenset()
        with pytest.raises(IllegalTransition):
            OPERATION_STATE_MACHINE.assert_transition(terminal, OperationStatus.RUNNING)


def test_table_matches_state_machine() -> None:
    every_status = set(OPERATION_TRANSITIONS) | set(OPERATION_TERMINAL)
    for current in every_status:
        for target in OperationStatus:
            match = target in OPERATION_TRANSITIONS.get(current, frozenset())
            assert OPERATION_STATE_MACHINE.can_transition(current, target) is match


def test_illegal_transition_raises() -> None:
    with pytest.raises(IllegalTransition):
        OPERATION_STATE_MACHINE.assert_transition(
            OperationStatus.QUEUED, OperationStatus.SUCCEEDED
        )
    with pytest.raises(IllegalTransition):
        OPERATION_STATE_MACHINE.assert_transition(
            OperationStatus.RUNNING, OperationStatus.QUEUED
        )
    with pytest.raises(IllegalTransition):
        OPERATION_STATE_MACHINE.assert_transition(
            OperationStatus.SUCCEEDED, OperationStatus.FAILED
        )


# -- cancellation semantics ----------------------------------------------------


def test_cancel_queued_moves_to_cancelled(tmp_path: Path) -> None:
    service, _ = _stack(tmp_path)
    op = _create_op(service)
    cancelled = service.cancel(op.operation_id, now=datetime(2026, 8, 24, 11, 0, tzinfo=UTC))
    assert cancelled.status == OperationStatus.CANCELLED
    assert cancelled.finished_at is not None


def test_cancel_running_requests_cancellation(tmp_path: Path) -> None:
    service, _ = _stack(tmp_path)
    op = _create_op(service)
    service.claim(op.operation_id, worker="worker-1", now=NOW)
    cancelled = service.cancel(op.operation_id, now=datetime(2026, 8, 24, 11, 0, tzinfo=UTC))
    assert cancelled.status == OperationStatus.CANCEL_REQUESTED
    assert cancelled.finished_at is None


def test_cancel_is_idempotent_for_cancelled(tmp_path: Path) -> None:
    service, _ = _stack(tmp_path)
    op = _create_op(service)
    service.cancel(op.operation_id, now=NOW)
    again = service.cancel(op.operation_id, now=datetime(2026, 8, 24, 11, 0, tzinfo=UTC))
    assert again.status == OperationStatus.CANCELLED
    # No extra state churn: intact operation id throughout.
    assert (
        service.get_operation(op.operation_id).operation_id == op.operation_id
    )


def test_cancel_is_idempotent_for_cancel_requested(tmp_path: Path) -> None:
    service, _ = _stack(tmp_path)
    op = _create_op(service)
    service.claim(op.operation_id, worker="worker-1", now=NOW)
    service.cancel(op.operation_id, now=NOW)
    again = service.cancel(op.operation_id, now=datetime(2026, 8, 24, 11, 0, tzinfo=UTC))
    assert again.status == OperationStatus.CANCEL_REQUESTED


def test_cancel_terminal_statuses_raise_not_cancellable(tmp_path: Path) -> None:
    service, _ = _stack(tmp_path)
    for terminal in (
        OperationStatus.SUCCEEDED,
        OperationStatus.FAILED,
        OperationStatus.UNCERTAIN,
    ):
        op = service.create_operation(
            kind=OperationKind.TARGET_TEST,
            target_id="tgt-a",
            created_by="alice",
            now=NOW,
        )
        service.claim(op.operation_id, worker="worker-1", now=NOW)
        service.transition(op.operation_id, terminal, now=NOW)
        with pytest.raises(OperationNotCancellable):
            service.cancel(op.operation_id, now=datetime(2026, 8, 24, 11, 0, tzinfo=UTC))


# -- event emission ------------------------------------------------------------


def test_create_emits_operation_queued_event(tmp_path: Path) -> None:
    service, events = _stack(tmp_path)
    op = _create_op(service, progress_summary="heartbeat ok", request_payload='{"secret":"x"}')
    entries = events.list_after(0)
    assert len(entries) == 1
    event = entries[0]
    assert event.event_type == RuntimeEventType.OPERATION_QUEUED
    assert event.payload["operation_id"] == op.operation_id
    assert event.payload["kind"] == "target_test"
    assert event.payload["status"] == "queued"
    assert event.payload["summary_preview"] == "heartbeat ok"
    # Events never carry request payloads.
    assert "request_payload" not in event.payload


def test_cancel_emits_cancelled_or_cancel_requested_event(tmp_path: Path) -> None:
    service, events = _stack(tmp_path)
    op = _create_op(service)

    service.cancel(op.operation_id, now=NOW)
    event_types = [event.event_type for event in events.list_after(0)]
    assert event_types == [
        RuntimeEventType.OPERATION_QUEUED,
        RuntimeEventType.OPERATION_CANCELLED,
    ]


def test_claim_and_terminal_transition_emit_events(tmp_path: Path) -> None:
    service, events = _stack(tmp_path)
    op = _create_op(service)
    service.claim(op.operation_id, worker="worker-1", now=NOW)
    service.transition(
        op.operation_id, OperationStatus.FAILED, error_message="boom", now=NOW
    )
    event_types = [event.event_type for event in events.list_after(0)]
    assert event_types == [
        RuntimeEventType.OPERATION_QUEUED,
        RuntimeEventType.OPERATION_RUNNING,
        RuntimeEventType.OPERATION_FAILED,
    ]


# -- safe, bounded, redacted errors --------------------------------------------


def test_error_message_is_redacted_and_bounded(tmp_path: Path) -> None:
    service, _ = _stack(tmp_path)
    op = _create_op(service)
    service.claim(op.operation_id, worker="worker-1", now=NOW)
    huge = "token=abcdefgh1234567890 " + ("x" * 5000)
    service.transition(
        op.operation_id,
        OperationStatus.FAILED,
        error_code="boom",
        error_message=huge,
        now=NOW,
    )
    stored = service.get_operation(op.operation_id)
    assert stored.error_message is not None
    assert len(stored.error_message) <= 2000
    assert "abcdefgh1234567890" not in stored.error_message
    assert "token=[REDACTED_TOKEN]" in stored.error_message


def test_request_payload_is_redacted_before_storage(tmp_path: Path) -> None:
    service, _ = _stack(tmp_path)
    op = _create_op(service, request_payload='{"password":"hunter2-hunter2"}')
    stored = service.get_operation(op.operation_id)
    assert stored.request_payload is not None
    assert "hunter2-hunter2" not in stored.request_payload
    assert "password=[REDACTED_PASSWORD]" in stored.request_payload
