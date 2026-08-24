"""Startup classification of durable operations after a process restart.

The harness reuses the real SQLite-backed runtime (operations store + service,
project registry, investigation store, change manager), so every recovery
scenario walks the real conditional-UPDATE transitions and the per-kind rules.

The core invariant: a dangerous ``running`` ROLLBACK is parked ``uncertain`` and
NEVER replayed -- ``changes.rollback_calls`` stays empty after a restart.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from incidentlens_control_plane.investigation.state_machine import InvestigationStatus
from incidentlens_control_plane.investigation.types import (
    Investigation,
    InvestigationBudget,
    UsageCounters,
)
from incidentlens_control_plane.operations.types import OperationKind, OperationStatus

NOW = datetime(2026, 8, 24, 10, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 24, 10, 5, 0, tzinfo=UTC)


def _enqueue(runtime, kind: OperationKind, **overrides):
    payload = {
        "kind": kind,
        "target_id": "tgt-a",
        "created_by": "alice",
        "now": NOW,
    }
    payload.update(overrides)
    return runtime.operations.enqueue(**payload)


def _transition_running(runtime, operation) -> None:
    runtime.operation_store.transition(
        runtime.operation_store.get(operation.operation_id),
        OperationStatus.RUNNING,
        now=NOW,
    )


def _recover(runtime, *, now: datetime = LATER):
    return asyncio.run(runtime.operation_recovery.recover(now=now))


def test_running_rollback_becomes_uncertain_after_restart_and_never_replays(
    runtime_factory,
) -> None:
    first = runtime_factory()
    operation = _enqueue(
        first,
        OperationKind.ROLLBACK,
        request_payload='{"changeset_id":"chs-1","approval_id":null}',
    )
    _transition_running(first, operation)

    second = runtime_factory()
    summary = _recover(second)

    restored = second.operation_store.get(operation.operation_id)
    assert restored.status == OperationStatus.UNCERTAIN
    assert "never replayed" in (restored.progress_summary or "")
    assert second.changes.rollback_calls == []
    assert summary.uncertain_rollbacks == 1
    assert summary.scanned == 1


def test_running_target_test_is_requeued_after_restart(runtime_factory) -> None:
    first = runtime_factory()
    operation = _enqueue(first, OperationKind.TARGET_TEST)
    _transition_running(first, operation)

    second = runtime_factory()
    summary = _recover(second)

    restored = second.operation_store.get(operation.operation_id)
    assert restored.status == OperationStatus.QUEUED
    assert restored.claim_token is None
    assert restored.claimed_at is None
    assert summary.requeued_target_tests == 1


def test_queued_work_survives_restart(runtime_factory) -> None:
    first = runtime_factory()
    queued = _enqueue(first, OperationKind.TARGET_TEST)
    running = _enqueue(first, OperationKind.TARGET_TEST, target_id="tgt-b")
    _transition_running(first, running)

    second = runtime_factory()
    summary = _recover(second)

    assert second.operation_store.get(queued.operation_id).status == OperationStatus.QUEUED
    assert second.operation_store.get(running.operation_id).status == OperationStatus.QUEUED
    assert summary.scanned == 2
    assert summary.requeued_target_tests == 1


def test_running_report_requeues_without_durable_result(runtime_factory) -> None:
    first = runtime_factory()
    operation = _enqueue(first, OperationKind.REPORT_GENERATE)
    _transition_running(first, operation)

    second = runtime_factory()
    summary = _recover(second)

    restored = second.operation_store.get(operation.operation_id)
    assert restored.status == OperationStatus.QUEUED
    assert restored.progress_summary is None
    assert summary.requeued_reports == 1


def test_agent_operation_reconciles_from_terminal_investigation(runtime_factory) -> None:
    first = runtime_factory()
    first.investigation_store.create_investigation(
        Investigation(
            investigation_id="inv-terminal",
            incident_id="inc-1",
            project_id="payments",
            target_id="dev-a",
            service="payment-api",
            symptom="down",
            status=InvestigationStatus.COMPLETED,
            budget=InvestigationBudget(),
            usage=UsageCounters(),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    operation = _enqueue(
        first,
        OperationKind.AGENT_MESSAGE,
        investigation_id="inv-terminal",
    )
    _transition_running(first, operation)

    second = runtime_factory()
    summary = _recover(second)

    restored = second.operation_store.get(operation.operation_id)
    assert restored.status == OperationStatus.SUCCEEDED
    assert summary.reconciled_agent_operations == 1


def test_agent_operation_is_requeued_when_linked_work_is_live(runtime_factory) -> None:
    first = runtime_factory()
    first.investigation_store.create_investigation(
        Investigation(
            investigation_id="inv-live",
            incident_id="inc-1",
            project_id="payments",
            target_id="dev-a",
            service="payment-api",
            symptom="down",
            status=InvestigationStatus.RUNNING,
            budget=InvestigationBudget(),
            usage=UsageCounters(),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    operation = _enqueue(
        first,
        OperationKind.INVESTIGATION_START,
        investigation_id="inv-live",
    )
    _transition_running(first, operation)

    second = runtime_factory()
    summary = _recover(second)

    restored = second.operation_store.get(operation.operation_id)
    assert restored.status == OperationStatus.QUEUED
    assert summary.reconciled_agent_operations == 1


def test_crash_mid_cancel_is_finalised_to_cancelled(runtime_factory) -> None:
    first = runtime_factory()
    operation = _enqueue(first, OperationKind.TARGET_TEST, target_id="tgt-c")
    _transition_running(first, operation)
    first.operation_store.transition(
        first.operation_store.get(operation.operation_id),
        OperationStatus.CANCEL_REQUESTED,
        now=NOW,
    )

    second = runtime_factory()
    summary = _recover(second)

    restored = second.operation_store.get(operation.operation_id)
    assert restored.status == OperationStatus.CANCELLED
    assert summary.finalised_cancels == 1


def test_recovery_is_safe_and_idempotent_on_consistent_store(runtime_factory) -> None:
    first = runtime_factory()
    queued = _enqueue(first, OperationKind.TARGET_TEST)  # stays queued

    second = runtime_factory()
    first_pass = _recover(second)
    second_pass = _recover(second)

    # Queued work is examined (scanned) but never transitioned: recovery is safe
    # to call on an already-consistent store and makes no moves.
    assert first_pass.scanned == 1
    assert first_pass.uncertain_rollbacks == 0
    assert first_pass.requeued_target_tests == 0
    assert second_pass.scanned == 1
    assert second_pass.uncertain_rollbacks == 0
    assert second.operation_store.get(queued.operation_id).status == OperationStatus.QUEUED
