"""Tests for evaluation metrics and runner — TDD RED phase.

Metrics must be computed from actual run records, never fixed scores.
Runner must execute real investigations and produce RunRecords.
"""

from __future__ import annotations

import pytest


def test_metrics_use_records_not_fixed_scores() -> None:
    """Core TDD test: compute_metrics derives values from records, not hardcoded."""
    from incidentlens_evaluation.metrics import RunRecord, compute_metrics

    result = compute_metrics([
        RunRecord(
            root_service_expected="payment-service",
            root_service_actual="payment-service",
            tool_calls=3,
            evidence_reference_correct=True,
            first_effective_round=1,
            duplicate_calls=0,
            misleading_calls=0,
            latency_ms=120.0,
        ),
        RunRecord(
            root_service_expected="order-service",
            root_service_actual="payment-service",
            tool_calls=5,
            evidence_reference_correct=False,
            first_effective_round=3,
            duplicate_calls=2,
            misleading_calls=1,
            latency_ms=250.0,
        ),
    ])
    assert result.root_service_accuracy == 0.5
    assert result.average_tool_calls == 4.0
    assert result.evidence_reference_correctness == 50.0
    assert result.first_effective_hypothesis_round == 2.0
    assert result.duplicate_rate == 0.25  # 2 / 8 total calls
    assert result.misleading_rate == 0.125  # 1 / 8 total calls
    assert result.average_latency_ms == 185.0


def test_compute_metrics_empty_records() -> None:
    """compute_metrics with empty records should return zeroed result."""
    from incidentlens_evaluation.metrics import EvaluationResult, compute_metrics

    result = compute_metrics([])
    assert result.root_service_accuracy == 0.0
    assert result.average_tool_calls == 0.0


def test_compute_metrics_single_perfect_record() -> None:
    """Single perfect record should yield 1.0 accuracy and 100% correctness."""
    from incidentlens_evaluation.metrics import RunRecord, compute_metrics

    result = compute_metrics([
        RunRecord(
            root_service_expected="payment-service",
            root_service_actual="payment-service",
            tool_calls=2,
            evidence_reference_correct=True,
            first_effective_round=1,
            duplicate_calls=0,
            misleading_calls=0,
            latency_ms=50.0,
        ),
    ])
    assert result.root_service_accuracy == 1.0
    assert result.evidence_reference_correctness == 100.0
    assert result.duplicate_rate == 0.0
    assert result.misleading_rate == 0.0


def test_run_evaluation_returns_result_from_actual_run() -> None:
    """run_evaluation should execute a real investigation and return metrics."""
    from incidentlens_evaluation.runner import run_evaluation

    result = run_evaluation("react_no_memory", "payment_delay")
    assert result.root_service_accuracy >= 0.0
    assert result.average_tool_calls >= 0.0


def test_run_single_produces_run_record() -> None:
    """run_single should produce a RunRecord from an actual investigation."""
    from incidentlens_evaluation.runner import run_single

    record = run_single("react_no_memory", "payment_delay")
    assert record.root_service_expected == "payment-service"
    assert record.tool_calls >= 0


def test_run_evaluation_rejects_invalid_strategy() -> None:
    """run_evaluation should reject unknown strategies."""
    from incidentlens_evaluation.runner import run_evaluation

    with pytest.raises(ValueError, match="Unknown strategy"):
        run_evaluation("invalid_strategy", "payment_delay")


def test_run_evaluation_rejects_invalid_scenario() -> None:
    """run_evaluation should reject unknown scenarios."""
    from incidentlens_evaluation.runner import run_evaluation

    with pytest.raises(ValueError, match="Unknown scenario"):
        run_evaluation("react_no_memory", "nonexistent_scenario")
