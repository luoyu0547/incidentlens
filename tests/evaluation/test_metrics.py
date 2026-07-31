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
            root_cause_type_expected="payment_latency_spike",
            root_cause_type_actual="payment_latency_spike",
            tool_calls=3,
            evidence_reference_correct=True,
            first_effective_round=1,
            duplicate_calls=0,
            historical_cases_adopted=0,
            historical_cases_misleading=0,
            latency_ms=120.0,
        ),
        RunRecord(
            root_service_expected="order-service",
            root_service_actual="payment-service",
            root_cause_type_expected="database_connection_leak",
            root_cause_type_actual="payment_service_degradation",
            tool_calls=5,
            evidence_reference_correct=False,
            first_effective_round=3,
            duplicate_calls=2,
            historical_cases_adopted=0,
            historical_cases_misleading=0,
            latency_ms=250.0,
        ),
    ])
    assert result.root_service_accuracy == 0.5
    assert result.root_cause_type_accuracy == 0.5
    assert result.average_tool_calls == 4.0
    assert result.evidence_reference_correctness == 50.0
    assert result.first_effective_hypothesis_round == 2.0
    assert result.duplicate_rate == 0.25  # 2 / 8 total calls
    assert result.historical_case_misleading_rate == 0.0  # no adopted cases
    assert result.average_latency_ms == 185.0


def test_compute_metrics_empty_records() -> None:
    """compute_metrics with empty records should return zeroed result."""
    from incidentlens_evaluation.metrics import compute_metrics

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
            root_cause_type_expected="payment_latency_spike",
            root_cause_type_actual="payment_latency_spike",
            tool_calls=2,
            evidence_reference_correct=True,
            first_effective_round=1,
            duplicate_calls=0,
            historical_cases_adopted=0,
            historical_cases_misleading=0,
            latency_ms=50.0,
        ),
    ])
    assert result.root_service_accuracy == 1.0
    assert result.root_cause_type_accuracy == 1.0
    assert result.evidence_reference_correctness == 100.0
    assert result.duplicate_rate == 0.0
    assert result.historical_case_misleading_rate == 0.0


def test_missing_report_is_not_counted_as_correct() -> None:
    """Records with no report (None actual) should not count as correct."""
    from incidentlens_evaluation.metrics import RunRecord, compute_metrics

    result = compute_metrics([
        RunRecord(
            root_service_expected="payment-service",
            root_service_actual=None,
            root_cause_type_expected="payment_latency_spike",
            root_cause_type_actual=None,
            tool_calls=0,
        ),
    ])
    assert result.root_service_accuracy == 0.0
    assert result.root_cause_type_accuracy == 0.0


def test_mixed_report_and_no_report_counts_missing_report_as_incorrect() -> None:
    """A missing report is an incorrect outcome in the full run denominator."""
    from incidentlens_evaluation.metrics import RunRecord, compute_metrics

    result = compute_metrics([
        RunRecord(
            root_service_expected="payment-service",
            root_service_actual="payment-service",
            root_cause_type_expected="payment_latency_spike",
            root_cause_type_actual="payment_latency_spike",
            tool_calls=3,
        ),
        RunRecord(
            root_service_expected="order-service",
            root_service_actual=None,
            root_cause_type_expected="database_connection_leak",
            root_cause_type_actual=None,
            tool_calls=0,
        ),
    ])
    assert result.root_service_accuracy == 0.5
    assert result.root_cause_type_accuracy == 0.5


def test_historical_case_misleading_rate_uses_adopted_cases() -> None:
    """historical_case_misleading_rate = misleading / adopted across all records."""
    from incidentlens_evaluation.metrics import RunRecord, compute_metrics

    result = compute_metrics([
        RunRecord(
            root_service_expected="payment-service",
            root_service_actual="payment-service",
            root_cause_type_expected="downstream-timeout",
            root_cause_type_actual="downstream-timeout",
            tool_calls=3,
            evidence_reference_correct=True,
            first_effective_round=1,
            duplicate_calls=0,
            historical_cases_adopted=2,
            historical_cases_misleading=1,
            latency_ms=100,
        )
    ])
    assert result.historical_case_misleading_rate == 0.5


def test_historical_case_misleading_rate_zero_adopted() -> None:
    """historical_case_misleading_rate is 0.0 when no cases adopted."""
    from incidentlens_evaluation.metrics import RunRecord, compute_metrics

    result = compute_metrics([
        RunRecord(
            root_service_expected="payment-service",
            root_service_actual="payment-service",
            root_cause_type_expected="downstream-timeout",
            root_cause_type_actual="downstream-timeout",
            tool_calls=3,
            historical_cases_adopted=0,
            historical_cases_misleading=0,
        )
    ])
    assert result.historical_case_misleading_rate == 0.0


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
