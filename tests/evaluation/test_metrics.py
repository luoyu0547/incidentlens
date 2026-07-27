"""Tests for evaluation metrics — TDD RED phase.

Metrics must be computed from actual run records, never fixed scores.
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
