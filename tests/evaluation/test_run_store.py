"""Tests for evaluation run store — TDD RED phase.

EvaluationRunStore persists evaluation runs and records, providing
latest_comparison for the two-strategy comparison API.
Strategies are deterministic_baseline and llm_agent.
"""

from __future__ import annotations

import pytest
from incidentlens_control_plane.evaluations.store import EvaluationRunStore
from sqlalchemy import create_engine


@pytest.fixture
def store() -> EvaluationRunStore:
    """Create an in-memory EvaluationRunStore for testing."""
    engine = create_engine("sqlite:///:memory:")
    return EvaluationRunStore(engine)


def test_start_returns_run_id(store: EvaluationRunStore) -> None:
    """start() should return a run ID."""
    run_id = store.start("deterministic_baseline", "all")
    assert run_id is not None
    assert isinstance(run_id, int)


def test_complete_requires_at_least_one_record(store: EvaluationRunStore) -> None:
    """complete() should fail if no records have been added."""
    run_id = store.start("deterministic_baseline", "all")
    with pytest.raises(ValueError, match="no records"):
        store.complete(run_id, {"root_service_accuracy": 1.0})


def test_record_adds_a_run_record(store: EvaluationRunStore) -> None:
    """record() should add a run record to the store."""
    run_id = store.start("deterministic_baseline", "all")
    store.record(run_id, {"root_service_expected": "payment-service"})
    # complete should now succeed
    store.complete(run_id, {"root_service_accuracy": 1.0})


def test_fail_marks_run_as_failed(store: EvaluationRunStore) -> None:
    """fail() should mark a run as failed with an error summary."""
    run_id = store.start("deterministic_baseline", "all")
    store.fail(run_id, "model_timeout")
    comparison = store.latest_comparison(scenario="all")
    assert comparison == []


def test_failed_run_is_not_returned_as_completed(store: EvaluationRunStore) -> None:
    """Failed runs should not appear in latest_comparison."""
    run_id = store.start("deterministic_baseline", "all")
    store.fail(run_id, "model_timeout")
    comparison = store.latest_comparison(scenario="all")
    assert comparison == []


def test_complete_stores_metrics_and_timestamp(store: EvaluationRunStore) -> None:
    """complete() should store metrics and completion timestamp."""
    run_id = store.start("deterministic_baseline", "payment_delay")
    store.record(run_id, {"root_service_expected": "payment-service"})
    store.complete(run_id, {"root_service_accuracy": 1.0})
    comparison = store.latest_comparison(scenario="payment_delay")
    assert len(comparison) == 1
    assert comparison[0]["metrics"]["root_service_accuracy"] == 1.0
    assert comparison[0]["strategy"] == "deterministic_baseline"
    assert comparison[0]["scenario"] == "payment_delay"


def test_latest_comparison_returns_only_completed(store: EvaluationRunStore) -> None:
    """latest_comparison should only return completed runs."""
    # Start and complete one run
    run_id1 = store.start("deterministic_baseline", "all")
    store.record(run_id1, {"root_service_expected": "payment-service"})
    store.complete(run_id1, {"root_service_accuracy": 0.5})

    # Start but don't complete another
    run_id2 = store.start("llm_agent", "all")
    store.record(run_id2, {"root_service_expected": "payment-service"})

    comparison = store.latest_comparison(scenario="all")
    assert len(comparison) == 1
    assert comparison[0]["strategy"] == "deterministic_baseline"


def test_latest_comparison_returns_latest_per_strategy(store: EvaluationRunStore) -> None:
    """latest_comparison should return only the latest completed run per strategy."""
    # First run for deterministic_baseline
    run_id1 = store.start("deterministic_baseline", "all")
    store.record(run_id1, {"root_service_expected": "payment-service"})
    store.complete(run_id1, {"root_service_accuracy": 0.5})

    # Second run for deterministic_baseline (should override the first)
    run_id2 = store.start("deterministic_baseline", "all")
    store.record(run_id2, {"root_service_expected": "order-service"})
    store.complete(run_id2, {"root_service_accuracy": 0.8})

    comparison = store.latest_comparison(scenario="all")
    assert len(comparison) == 1
    assert comparison[0]["metrics"]["root_service_accuracy"] == 0.8


def test_latest_comparison_filters_by_scenario(store: EvaluationRunStore) -> None:
    """latest_comparison should filter by scenario."""
    run_id1 = store.start("deterministic_baseline", "payment_delay")
    store.record(run_id1, {"root_service_expected": "payment-service"})
    store.complete(run_id1, {"root_service_accuracy": 0.5})

    run_id2 = store.start("deterministic_baseline", "db_pool_exhaustion")
    store.record(run_id2, {"root_service_expected": "db-service"})
    store.complete(run_id2, {"root_service_accuracy": 0.3})

    comparison_payment = store.latest_comparison(scenario="payment_delay")
    assert len(comparison_payment) == 1
    assert comparison_payment[0]["scenario"] == "payment_delay"

    # When filtering by "all", we get one run per strategy (latest completed)
    # Since both runs have the same strategy, only the latest one is returned
    comparison_all = store.latest_comparison(scenario="all")
    assert len(comparison_all) == 1
    assert comparison_all[0]["strategy"] == "deterministic_baseline"


def test_cannot_complete_already_failed_run(store: EvaluationRunStore) -> None:
    """complete() should reject completing an already failed run."""
    run_id = store.start("deterministic_baseline", "all")
    store.fail(run_id, "model_timeout")
    with pytest.raises(ValueError, match="already failed"):
        store.complete(run_id, {"root_service_accuracy": 1.0})


def test_empty_comparison_when_no_completed_runs(store: EvaluationRunStore) -> None:
    """latest_comparison should return empty list when no completed runs."""
    comparison = store.latest_comparison(scenario="all")
    assert comparison == []
