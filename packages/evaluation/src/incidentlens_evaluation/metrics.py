"""Evaluation metrics computed from actual run records.

Metrics:
  - root_service_accuracy: fraction of runs where actual matches expected
  - root_cause_type_accuracy: fraction of runs where actual cause matches expected
  - evidence_reference_correctness: percentage of runs with correct evidence refs
  - first_effective_hypothesis_round: average round where first effective hypothesis found
  - average_tool_calls: mean tool calls across runs
  - duplicate_rate: fraction of total calls that are duplicates
  - historical_case_misleading_rate: misleading cases / adopted cases across all records
  - average_latency_ms: mean latency across runs

All values are derived from RunRecord instances — never hardcoded.
"""

from __future__ import annotations

from pydantic import BaseModel


class RunRecord(BaseModel):
    """Record of a single evaluation run outcome.

    Attributes:
        root_service_expected: the true root cause service (from scenario definition)
        root_service_actual: the service identified by the investigation (None if no report)
        root_cause_type_expected: the expected cause code (from scenario definition)
        root_cause_type_actual: the cause code identified by the investigation (None if no report)
        tool_calls: total number of tool calls made during the investigation
        evidence_reference_correct: whether evidence correctly references the root cause
        first_effective_round: the round number where the first effective hypothesis appeared
        duplicate_calls: number of duplicate (same tool+args) calls
        historical_cases_adopted: number of historical cases adopted during investigation
        historical_cases_misleading: number of historical cases that were misleading
        latency_ms: total investigation latency in milliseconds
    """

    root_service_expected: str
    root_service_actual: str | None
    root_cause_type_expected: str
    root_cause_type_actual: str | None
    tool_calls: int
    evidence_reference_correct: bool = False
    first_effective_round: int | None = None
    duplicate_calls: int = 0
    historical_cases_adopted: int = 0
    historical_cases_misleading: int = 0
    latency_ms: float = 0.0


class EvaluationResult(BaseModel):
    """Aggregated evaluation metrics computed from run records.

    All values are derived from actual records — never fixed scores.
    """

    root_service_accuracy: float = 0.0
    root_cause_type_accuracy: float = 0.0
    evidence_reference_correctness: float = 0.0
    first_effective_hypothesis_round: float = 0.0
    average_tool_calls: float = 0.0
    duplicate_rate: float = 0.0
    historical_case_misleading_rate: float = 0.0
    average_latency_ms: float = 0.0


def compute_metrics(records: list[RunRecord]) -> EvaluationResult:
    """Compute evaluation metrics from a list of run records.

    All metrics are derived from the actual records — no fixed scores.
    Returns zeroed EvaluationResult for empty input.
    """
    if not records:
        return EvaluationResult()

    n = len(records)

    # Accuracy uses every run as the denominator. A missing report is a failed
    # outcome, rather than being silently excluded from the score.
    service_correct = sum(
        1 for r in records
        if r.root_service_actual is not None
        and r.root_service_actual == r.root_service_expected
    )
    root_service_accuracy = service_correct / n

    cause_correct = sum(
        1 for r in records
        if r.root_cause_type_actual is not None
        and r.root_cause_type_actual == r.root_cause_type_expected
    )
    root_cause_type_accuracy = cause_correct / n

    # Evidence reference correctness: percentage of runs with correct refs
    correct_refs = sum(1 for r in records if r.evidence_reference_correct)
    evidence_reference_correctness = (correct_refs / n) * 100.0

    # First effective hypothesis round: average across runs (None means none found)
    effective_rounds = [
        r.first_effective_round for r in records
        if r.first_effective_round is not None and r.first_effective_round > 0
    ]
    first_effective_hypothesis_round = (
        sum(effective_rounds) / len(effective_rounds) if effective_rounds else 0.0
    )

    # Average tool calls
    total_calls = sum(r.tool_calls for r in records)
    average_tool_calls = total_calls / n

    # Duplicate rate: fraction of total calls that are duplicates
    total_duplicate = sum(r.duplicate_calls for r in records)
    duplicate_rate = total_duplicate / total_calls if total_calls > 0 else 0.0

    # Historical case misleading rate: misleading / adopted across all records
    total_adopted = sum(r.historical_cases_adopted for r in records)
    total_misleading = sum(r.historical_cases_misleading for r in records)
    historical_case_misleading_rate = (
        total_misleading / total_adopted if total_adopted > 0 else 0.0
    )

    # Average latency
    average_latency_ms = sum(r.latency_ms for r in records) / n

    return EvaluationResult(
        root_service_accuracy=root_service_accuracy,
        root_cause_type_accuracy=root_cause_type_accuracy,
        evidence_reference_correctness=evidence_reference_correctness,
        first_effective_hypothesis_round=first_effective_hypothesis_round,
        average_tool_calls=average_tool_calls,
        duplicate_rate=duplicate_rate,
        historical_case_misleading_rate=historical_case_misleading_rate,
        average_latency_ms=average_latency_ms,
    )
