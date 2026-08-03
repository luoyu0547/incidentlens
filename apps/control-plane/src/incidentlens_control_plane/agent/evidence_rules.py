"""Deterministic evidence assessment rules for root-service identification.

Public interface:
  - EvidenceAssessment(candidate_service, root_cause, supports, contradicts)
  - assess_evidence(evidence) -> list[EvidenceAssessment]

The engine consumes persisted Evidence only; it may NOT import scenario
definitions (no root_cause_label access).  Instead, evidence content
patterns are matched deterministically to candidate services and cause
codes.

Five mappings:
  1. payment_delay       -> payment-service / payment_latency_spike
  2. payment_error_rate  -> payment-service / payment_service_degradation
  3. db_pool_exhaustion  -> order-service  / database_connection_leak
  4. dependency_unavailable -> order-service / network_partition
  5. deployment_regression  -> payment-service / bad_deployment
"""

from __future__ import annotations

from typing import Any

from incidentlens_contracts.models import Evidence
from pydantic import BaseModel


class EvidenceAssessment(BaseModel):
    """Result of assessing a single evidence item against known patterns.

    Attributes:
        candidate_service: The service most likely responsible.
        root_cause: A cause code (e.g. "payment_latency_spike").
        supports: Whether this evidence supports the candidate.
        contradicts: Whether this evidence contradicts the candidate.
    """

    candidate_service: str
    root_cause: str
    supports: bool = True
    contradicts: bool = False


# ---------------------------------------------------------------------------
# Pattern matchers — each returns a list of EvidenceAssessment or empty list
# ---------------------------------------------------------------------------


def _assess_log_item(item: dict[str, Any]) -> list[EvidenceAssessment]:
    """Assess a log item from search_logs evidence."""
    service = item.get("service", "")
    level = item.get("level", "")
    message = str(item.get("message", "")).lower()

    assessments: list[EvidenceAssessment] = []

    # Payment-service latency patterns
    if service == "payment-service" and level == "ERROR":
        if any(kw in message for kw in ("delay", "latency", "slow", "timeout")):
            assessments.append(
                EvidenceAssessment(
                    candidate_service="payment-service",
                    root_cause="payment_latency_spike",
                    supports=True,
                )
            )
        if any(kw in message for kw in ("error rate", "injected error", "failed", "500")):
            assessments.append(
                EvidenceAssessment(
                    candidate_service="payment-service",
                    root_cause="payment_service_degradation",
                    supports=True,
                )
            )

    # Payment-service normal operation contradicts latency spike
    if service == "payment-service" and level in ("INFO", "WARN"):
        if any(kw in message for kw in ("normal", "ok", "healthy", "fast", "success", "completed")):
            assessments.append(
                EvidenceAssessment(
                    candidate_service="payment-service",
                    root_cause="payment_latency_spike",
                    supports=False,
                    contradicts=True,
                )
            )
            assessments.append(
                EvidenceAssessment(
                    candidate_service="payment-service",
                    root_cause="payment_service_degradation",
                    supports=False,
                    contradicts=True,
                )
            )

    # Order-service connection pool patterns. The service emits expected pool
    # saturation as WARN because the request remains recoverable.
    if service == "order-service" and level in ("ERROR", "WARN"):
        if any(kw in message for kw in ("pool", "connection", "exhaust", "acquire")):
            assessments.append(
                EvidenceAssessment(
                    candidate_service="order-service",
                    root_cause="database_connection_leak",
                    supports=True,
                )
            )
        if level == "ERROR" and any(
            kw in message for kw in ("dependency", "unavailable", "502", "upstream")
        ):
            assessments.append(
                EvidenceAssessment(
                    candidate_service="order-service",
                    root_cause="network_partition",
                    supports=True,
                )
            )

    # Order-service healthy operation contradicts db leak and network partition
    if service == "order-service" and level in ("INFO", "WARN"):
        if any(
            kw in message
            for kw in ("pool ok", "healthy pool", "available", "connected", "success")
        ):
            assessments.append(
                EvidenceAssessment(
                    candidate_service="order-service",
                    root_cause="database_connection_leak",
                    supports=False,
                    contradicts=True,
                )
            )
        if any(kw in message for kw in ("reachable", "available", "success", "connected")):
            assessments.append(
                EvidenceAssessment(
                    candidate_service="order-service",
                    root_cause="network_partition",
                    supports=False,
                    contradicts=True,
                )
            )

    return assessments


def _assess_metric_item(item: dict[str, Any]) -> list[EvidenceAssessment]:
    """Assess a metric item from query_metrics evidence."""
    service = item.get("service", "")
    name = str(item.get("name", "")).lower()
    value = item.get("value")

    assessments: list[EvidenceAssessment] = []

    # Payment-service error rate
    if service == "payment-service":
        if "error_rate" in name and isinstance(value, (int, float)) and value > 0.1:
            assessments.append(
                EvidenceAssessment(
                    candidate_service="payment-service",
                    root_cause="payment_service_degradation",
                    supports=True,
                )
            )
        if "latency" in name and isinstance(value, (int, float)) and value > 200:
            assessments.append(
                EvidenceAssessment(
                    candidate_service="payment-service",
                    root_cause="payment_latency_spike",
                    supports=True,
                )
            )
        # Low error rate contradicts payment_service_degradation
        if "error_rate" in name and isinstance(value, (int, float)) and value <= 0.05:
            assessments.append(
                EvidenceAssessment(
                    candidate_service="payment-service",
                    root_cause="payment_service_degradation",
                    supports=False,
                    contradicts=True,
                )
            )
        # Normal latency contradicts payment_latency_spike
        if "latency" in name and isinstance(value, (int, float)) and value <= 100:
            assessments.append(
                EvidenceAssessment(
                    candidate_service="payment-service",
                    root_cause="payment_latency_spike",
                    supports=False,
                    contradicts=True,
                )
            )

    # Order-service pool metrics
    if service == "order-service":
        if "pool" in name and isinstance(value, (int, float)) and value > 10:
            assessments.append(
                EvidenceAssessment(
                    candidate_service="order-service",
                    root_cause="database_connection_leak",
                    supports=True,
                )
            )
        # Healthy pool metrics contradict database_connection_leak
        if "pool" in name and isinstance(value, (int, float)) and value <= 5:
            assessments.append(
                EvidenceAssessment(
                    candidate_service="order-service",
                    root_cause="database_connection_leak",
                    supports=False,
                    contradicts=True,
                )
            )

    return assessments


def _assess_slow_trace_item(item: dict[str, Any]) -> list[EvidenceAssessment]:
    """Assess a slow trace item from get_slow_traces evidence.

    GetSlowTracesTool returns items with {trace_id, duration_seconds, span_count}
    but no service field.  Since slow traces are typically invoked while
    investigating a specific service, we match any trace with high duration
    as supporting evidence for payment_latency_spike.
    """
    duration = item.get("duration_seconds")

    assessments: list[EvidenceAssessment] = []

    # Match by duration threshold — slow traces indicate latency issues
    if isinstance(duration, (int, float)) and duration > 5:
        assessments.append(
            EvidenceAssessment(
                candidate_service="payment-service",
                root_cause="payment_latency_spike",
                supports=True,
            )
        )

    return assessments


def _assess_deployment_item(item: dict[str, Any]) -> list[EvidenceAssessment]:
    """Assess a deployment item from list_recent_deployments evidence."""
    service = item.get("service", "")
    version = str(item.get("version", "")).lower()

    assessments: list[EvidenceAssessment] = []

    if service == "payment-service" and version:
        # Stable/same version deployment contradicts bad_deployment
        if "same" in version:
            assessments.append(
                EvidenceAssessment(
                    candidate_service="payment-service",
                    root_cause="bad_deployment",
                    supports=False,
                    contradicts=True,
                )
            )
        else:
            # Any other version is a candidate for bad_deployment
            assessments.append(
                EvidenceAssessment(
                    candidate_service="payment-service",
                    root_cause="bad_deployment",
                    supports=True,
                )
            )

    return assessments


def _assess_dependency_item(item: dict[str, Any]) -> list[EvidenceAssessment]:
    """Assess a dependency item from get_service_dependencies evidence."""
    from_service = item.get("from", "")
    to_service = item.get("to", "")

    assessments: list[EvidenceAssessment] = []

    # Order-service depending on payment-service
    if from_service == "order-service" and to_service == "payment-service":
        assessments.append(
            EvidenceAssessment(
                candidate_service="order-service",
                root_cause="network_partition",
                supports=True,
            )
        )

    return assessments


# ---------------------------------------------------------------------------
# Item dispatcher by source tool
# ---------------------------------------------------------------------------

_ASSESSORS: dict[str, Any] = {
    "search_logs": _assess_log_item,
    "query_metrics": _assess_metric_item,
    "get_slow_traces": _assess_slow_trace_item,
    "list_recent_deployments": _assess_deployment_item,
    "get_service_dependencies": _assess_dependency_item,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def assess_evidence(evidence: Evidence) -> list[EvidenceAssessment]:
    """Assess evidence items and return deterministic assessments.

    Each item in the evidence content is evaluated against known patterns.
    Returns a list of EvidenceAssessment objects for each matching pattern.
    Items that don't match any pattern produce no assessments.
    """
    items = evidence.content.get("items", [])
    assessor = _ASSESSORS.get(evidence.source_tool)

    if assessor is None:
        return []

    return [assessment for item in items for assessment in assessor(item)]
