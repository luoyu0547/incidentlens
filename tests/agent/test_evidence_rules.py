"""Tests for evidence rules and guarded root-service reports — TDD RED phase.

Tests cover:
  - assess_evidence maps evidence patterns to candidate services and cause codes
  - Five deterministic mappings: payment_delay, payment_error_rate,
    db_pool_exhaustion, dependency_unavailable, deployment_regression
  - Report guard requires root_service and incident-owned evidence_ids
  - Report contains root_service, root_cause (cause_code), evidence_ids
  - root_cause_label must never appear in reports or API output
"""

from __future__ import annotations

from uuid import uuid4

from incidentlens_contracts.models import (
    Evidence,
    Hypothesis,
    HypothesisStatus,
    InvestigationStatus,
)
from incidentlens_control_plane.agent.reporting import can_generate_report, generate_report
from incidentlens_control_plane.agent.state import InvestigationState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_evidence(
    source_tool: str = "search_logs",
    tool_call_id: str = "call-1",
    content: dict | None = None,
) -> Evidence:
    return Evidence(
        id=str(uuid4()),
        source_tool=source_tool,
        tool_call_id=tool_call_id,
        content=content or {},
        supports_hypothesis_ids=[],
        contradicts_hypothesis_ids=[],
    )


def _make_state(
    hypotheses: list[Hypothesis] | None = None,
    evidence: list[Evidence] | None = None,
) -> InvestigationState:
    return InvestigationState(
        incident_id=str(uuid4()),
        status=InvestigationStatus.INVESTIGATING,
        alert={"service": "payment-service", "error_rate": 0.3},
        hypotheses=hypotheses or [],
        evidence=evidence or [],
    )


# ===================================================================
# EVIDENCE RULES: assess_evidence deterministic mappings
# ===================================================================


class TestAssessEvidencePaymentDelay:
    """Payment delay scenario: payment-service with latency evidence."""

    def test_payment_latency_logs_creates_payment_candidate(self) -> None:
        """Log evidence with payment-service latency maps to payment-service
        / payment_latency_spike."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="search_logs",
            content={
                "items": [
                    {
                        "service": "payment-service",
                        "level": "ERROR",
                        "message": "Payment processing delayed due to injected latency",
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        assert len(assessments) > 0
        assert assessments[0].candidate_service == "payment-service"
        assert assessments[0].root_cause == "payment_latency_spike"
        assert assessments[0].supports is True

    def test_slow_traces_payment_service_creates_payment_latency(self) -> None:
        """Slow traces with high duration maps to payment-service / payment_latency_spike.

        GetSlowTracesTool returns items with {trace_id, duration_seconds, span_count}
        but no service field.  The assessor matches by duration threshold.
        """
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="get_slow_traces",
            content={
                "items": [
                    {
                        "trace_id": "trace-1",
                        "duration_seconds": 12.5,
                        "span_count": 3,
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        assert len(assessments) > 0
        assert assessments[0].candidate_service == "payment-service"
        assert assessments[0].root_cause == "payment_latency_spike"


class TestAssessEvidencePaymentErrorRate:
    """Payment error rate scenario: payment-service with error rate evidence."""

    def test_payment_error_log_creates_payment_candidate(self) -> None:
        """Log evidence with payment-service error rate maps to
        payment-service / payment_service_degradation."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="search_logs",
            tool_call_id="call-1",
            content={
                "items": [
                    {
                        "service": "payment-service",
                        "level": "ERROR",
                        "message": "Payment failed due to injected error rate",
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        assert len(assessments) > 0
        assert assessments[0].candidate_service == "payment-service"
        assert assessments[0].root_cause == "payment_service_degradation"

    def test_high_error_rate_metrics_creates_payment_degradation(self) -> None:
        """High error rate metrics for payment-service maps to payment_service_degradation."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="query_metrics",
            content={
                "items": [
                    {
                        "service": "payment-service",
                        "name": "error_rate",
                        "value": 0.3,
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        assert len(assessments) > 0
        assert assessments[0].candidate_service == "payment-service"
        assert assessments[0].root_cause == "payment_service_degradation"


class TestAssessEvidenceDBPoolExhaustion:
    """DB pool exhaustion scenario: order-service with connection pool evidence."""

    def test_db_pool_exhaustion_logs_creates_order_candidate(self) -> None:
        """Log evidence with connection pool exhaustion maps to
        order-service / database_connection_leak."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="search_logs",
            content={
                "items": [
                    {
                        "service": "order-service",
                        "level": "ERROR",
                        "message": "Connection pool exhausted, unable to acquire connection",
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        assert len(assessments) > 0
        assert assessments[0].candidate_service == "order-service"
        assert assessments[0].root_cause == "database_connection_leak"

    def test_db_pool_exhaustion_warning_creates_order_candidate(self) -> None:
        """The real order service emits pool exhaustion as a warning."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="search_logs",
            content={
                "items": [
                    {
                        "service": "order-service",
                        "level": "WARN",
                        "message": "DB pool exhausted, simulating slow query",
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        assert [(item.candidate_service, item.root_cause) for item in assessments] == [
            ("order-service", "database_connection_leak")
        ]

    def test_db_pool_metrics_creates_order_candidate(self) -> None:
        """Metrics showing pool exhaustion for order-service maps to database_connection_leak."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="query_metrics",
            content={
                "items": [
                    {
                        "service": "order-service",
                        "name": "db_pool_active",
                        "value": 50,
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        assert len(assessments) > 0
        assert assessments[0].candidate_service == "order-service"
        assert assessments[0].root_cause == "database_connection_leak"


class TestAssessEvidenceDependencyUnavailable:
    """Dependency unavailable scenario: order-service can't reach payment-service."""

    def test_dependency_failure_logs_creates_order_candidate(self) -> None:
        """Log evidence with dependency failure maps to order-service / network_partition."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="search_logs",
            content={
                "items": [
                    {
                        "service": "order-service",
                        "level": "ERROR",
                        "message": "Dependency unavailable: payment-service returned 502",
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        assert len(assessments) > 0
        assert assessments[0].candidate_service == "order-service"
        assert assessments[0].root_cause == "network_partition"

    def test_dependency_graph_shows_unavailable(self) -> None:
        """Service dependency evidence showing order->payment maps to network_partition."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="get_service_dependencies",
            content={
                "items": [
                    {"from": "order-service", "to": "payment-service"},
                ]
            },
        )
        assessments = assess_evidence(evidence)
        assert len(assessments) > 0
        assert assessments[0].candidate_service == "order-service"
        assert assessments[0].root_cause == "network_partition"


class TestAssessEvidenceDeploymentRegression:
    """Deployment regression scenario: payment-service with bad deployment."""

    def test_deployment_regression_creates_payment_candidate(self) -> None:
        """Deployment evidence with buggy version maps to payment-service / bad_deployment."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="list_recent_deployments",
            content={
                "items": [
                    {
                        "service": "payment-service",
                        "version": "v2.0.0-buggy",
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        assert len(assessments) > 0
        assert assessments[0].candidate_service == "payment-service"
        assert assessments[0].root_cause == "bad_deployment"

    def test_deployment_with_errors_creates_bad_deployment(self) -> None:
        """Deployment evidence combined with error logs produces
        bad_deployment for payment-service."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        # Deployment evidence with a non-buggy version (just a recent deploy)
        deployment_evidence = _make_evidence(
            source_tool="list_recent_deployments",
            content={
                "items": [
                    {
                        "service": "payment-service",
                        "version": "v2.0.0",
                    }
                ]
            },
        )
        deployment_assessments = assess_evidence(deployment_evidence)
        assert len(deployment_assessments) > 0
        assert deployment_assessments[0].candidate_service == "payment-service"
        assert deployment_assessments[0].root_cause == "bad_deployment"

        # Error log evidence corroborates the deployment issue
        error_evidence = _make_evidence(
            source_tool="search_logs",
            content={
                "items": [
                    {
                        "service": "payment-service",
                        "level": "ERROR",
                        "message": "Payment processing delay after deployment",
                    }
                ]
            },
        )
        error_assessments = assess_evidence(error_evidence)
        assert len(error_assessments) > 0
        # Both deployment and error evidence point to payment-service
        assert error_assessments[0].candidate_service == "payment-service"


class TestAssessEvidenceNoMatch:
    """Evidence that doesn't match any pattern returns empty assessments."""

    def test_unrelated_evidence_returns_empty(self) -> None:
        """Evidence with no matching pattern returns empty list."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="search_logs",
            content={
                "items": [
                    {
                        "service": "gateway-service",
                        "level": "INFO",
                        "message": "Request routed successfully",
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        assert len(assessments) == 0

    def test_empty_evidence_returns_empty(self) -> None:
        """Evidence with no items returns empty list."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="search_logs",
            content={"count": 0, "items": []},
        )
        assessments = assess_evidence(evidence)
        assert len(assessments) == 0


# ===================================================================
# REPORT GUARD: requires root_service and incident-owned evidence_ids
# ===================================================================


class TestReportGuardRootService:
    """Report guard requires root_service on confirmed hypothesis."""

    def test_report_rejects_missing_root_service(self) -> None:
        """Report cannot be generated if confirmed hypothesis has no root_service."""
        ev = _make_evidence()
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Payment service is slow",
            confidence=0.85,
            supporting_evidence_ids=[ev.id],
            contradicting_evidence_ids=[],
            status=HypothesisStatus.CONFIRMED,
            root_service="",
            cause_code="",
        )
        state = _make_state(hypotheses=[hyp], evidence=[ev])
        assert can_generate_report(state) is False

    def test_report_accepts_with_root_service(self) -> None:
        """Report can be generated when confirmed hypothesis has root_service and owned evidence."""
        ev = _make_evidence()
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Payment service is slow",
            confidence=0.85,
            supporting_evidence_ids=[ev.id],
            contradicting_evidence_ids=[],
            status=HypothesisStatus.CONFIRMED,
            root_service="payment-service",
            cause_code="payment_latency_spike",
        )
        state = _make_state(hypotheses=[hyp], evidence=[ev])
        assert can_generate_report(state) is True


class TestReportGuardEvidenceOwnership:
    """Report guard requires evidence_ids to be owned by the current incident."""

    def test_report_rejects_evidence_not_owned_by_incident(self) -> None:
        """Report cannot be generated if supporting evidence is not in the
        incident's evidence list."""
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Payment service is slow",
            confidence=0.85,
            supporting_evidence_ids=["not-in-this-incident"],
            contradicting_evidence_ids=[],
            status=HypothesisStatus.CONFIRMED,
            root_service="payment-service",
            cause_code="payment_latency_spike",
        )
        # State has no evidence matching the hypothesis's supporting_evidence_ids
        state = _make_state(hypotheses=[hyp], evidence=[])
        assert can_generate_report(state) is False

    def test_report_accepts_owned_evidence(self) -> None:
        """Report can be generated when all supporting evidence is owned by the incident."""
        ev = _make_evidence()
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Payment service is slow",
            confidence=0.85,
            supporting_evidence_ids=[ev.id],
            contradicting_evidence_ids=[],
            status=HypothesisStatus.CONFIRMED,
            root_service="payment-service",
            cause_code="payment_latency_spike",
        )
        state = _make_state(hypotheses=[hyp], evidence=[ev])
        assert can_generate_report(state) is True


class TestReportGuardNoConfirmedHypothesis:
    """Report guard requires at least one confirmed hypothesis."""

    def test_report_rejects_no_confirmed_hypothesis(self) -> None:
        """Report cannot be generated without a confirmed hypothesis."""
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Payment service is slow",
            confidence=0.5,
            status=HypothesisStatus.ACTIVE,
            root_service="payment-service",
            cause_code="payment_latency_spike",
        )
        state = _make_state(hypotheses=[hyp], evidence=[])
        assert can_generate_report(state) is False


class TestReportGuardNonEmptyEvidenceIds:
    """Report guard requires non-empty supporting_evidence_ids on confirmed hypothesis."""

    def test_report_rejects_empty_evidence_ids(self) -> None:
        """Report cannot be generated if confirmed hypothesis has empty supporting_evidence_ids."""
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Payment service is slow",
            confidence=0.85,
            supporting_evidence_ids=[],
            contradicting_evidence_ids=[],
            status=HypothesisStatus.CONFIRMED,
            root_service="payment-service",
            cause_code="payment_latency_spike",
        )
        state = _make_state(hypotheses=[hyp], evidence=[])
        assert can_generate_report(state) is False

    def test_report_rejects_empty_evidence_ids_even_with_evidence_present(self) -> None:
        """Report cannot be generated if confirmed hypothesis has empty evidence_ids,
        even when the state has other evidence present."""
        ev = _make_evidence()
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Payment service is slow",
            confidence=0.85,
            supporting_evidence_ids=[],
            contradicting_evidence_ids=[],
            status=HypothesisStatus.CONFIRMED,
            root_service="payment-service",
            cause_code="payment_latency_spike",
        )
        state = _make_state(hypotheses=[hyp], evidence=[ev])
        assert can_generate_report(state) is False


# ===================================================================
# REPORT FORMAT: contains root_service, root_cause, evidence_ids
# ===================================================================


class TestReportFormat:
    """Report must contain root_service, root_cause (cause_code), evidence_ids."""

    def test_report_contains_root_service(self) -> None:
        """Generated report must include root_service field."""
        ev = _make_evidence()
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Payment service is slow",
            confidence=0.85,
            supporting_evidence_ids=[ev.id],
            contradicting_evidence_ids=[],
            status=HypothesisStatus.CONFIRMED,
            root_service="payment-service",
            cause_code="payment_latency_spike",
        )
        state = _make_state(hypotheses=[hyp], evidence=[ev])
        report = generate_report(state)
        assert "root_service" in report
        assert report["root_service"] == "payment-service"

    def test_report_contains_cause_code(self) -> None:
        """Generated report must include root_cause (cause_code) field."""
        ev = _make_evidence()
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Payment service is slow",
            confidence=0.85,
            supporting_evidence_ids=[ev.id],
            contradicting_evidence_ids=[],
            status=HypothesisStatus.CONFIRMED,
            root_service="payment-service",
            cause_code="payment_latency_spike",
        )
        state = _make_state(hypotheses=[hyp], evidence=[ev])
        report = generate_report(state)
        assert "root_cause" in report
        assert report["root_cause"] == "payment_latency_spike"

    def test_report_contains_evidence_ids(self) -> None:
        """Generated report must include evidence_ids field."""
        ev = _make_evidence()
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Payment service is slow",
            confidence=0.85,
            supporting_evidence_ids=[ev.id],
            contradicting_evidence_ids=[],
            status=HypothesisStatus.CONFIRMED,
            root_service="payment-service",
            cause_code="payment_latency_spike",
        )
        state = _make_state(hypotheses=[hyp], evidence=[ev])
        report = generate_report(state)
        assert "evidence_ids" in report
        assert ev.id in report["evidence_ids"]

    def test_report_never_contains_root_cause_label(self) -> None:
        """Report must never contain root_cause_label (internal-only field)."""
        ev = _make_evidence()
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Payment service is slow",
            confidence=0.85,
            supporting_evidence_ids=[ev.id],
            contradicting_evidence_ids=[],
            status=HypothesisStatus.CONFIRMED,
            root_service="payment-service",
            cause_code="payment_latency_spike",
        )
        state = _make_state(hypotheses=[hyp], evidence=[ev])
        report = generate_report(state)
        report_str = str(report)
        assert "root_cause_label" not in report_str


# ===================================================================
# HYPOTHESIS MODEL: root_service and cause_code fields
# ===================================================================


class TestHypothesisFields:
    """Hypothesis model must have root_service and cause_code fields."""

    def test_hypothesis_has_root_service_field(self) -> None:
        """Hypothesis model must include root_service field."""
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Test hypothesis",
            root_service="payment-service",
        )
        assert hyp.root_service == "payment-service"

    def test_hypothesis_has_cause_code_field(self) -> None:
        """Hypothesis model must include cause_code field."""
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Test hypothesis",
            cause_code="payment_latency_spike",
        )
        assert hyp.cause_code == "payment_latency_spike"

    def test_hypothesis_default_root_service_empty(self) -> None:
        """Hypothesis root_service defaults to empty string."""
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Test hypothesis",
        )
        assert hyp.root_service == ""

    def test_hypothesis_default_cause_code_empty(self) -> None:
        """Hypothesis cause_code defaults to empty string."""
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Test hypothesis",
        )
        assert hyp.cause_code == ""


# ===================================================================
# EVIDENCE ASSESSMENT MODEL: EvidenceAssessment fields
# ===================================================================


class TestEvidenceAssessmentModel:
    """EvidenceAssessment model must have the expected fields."""

    def test_evidence_assessment_creation(self) -> None:
        """EvidenceAssessment can be created with all fields."""
        from incidentlens_control_plane.agent.evidence_rules import EvidenceAssessment

        assessment = EvidenceAssessment(
            candidate_service="payment-service",
            root_cause="payment_latency_spike",
            supports=True,
            contradicts=False,
        )
        assert assessment.candidate_service == "payment-service"
        assert assessment.root_cause == "payment_latency_spike"
        assert assessment.supports is True
        assert assessment.contradicts is False

    def test_evidence_assessment_defaults(self) -> None:
        """EvidenceAssessment supports=True and contradicts=False by default."""
        from incidentlens_control_plane.agent.evidence_rules import EvidenceAssessment

        assessment = EvidenceAssessment(
            candidate_service="order-service",
            root_cause="database_connection_leak",
        )
        assert assessment.supports is True
        assert assessment.contradicts is False


# ===================================================================
# REPORT GUARD: partial evidence ownership
# ===================================================================


class TestReportGuardPartialEvidence:
    """Report guard with partially owned evidence."""

    def test_report_rejects_partially_owned_evidence(self) -> None:
        """Report cannot be generated if some supporting evidence is not owned."""
        ev_owned = _make_evidence()
        ev_foreign_id = "foreign-evidence-id"
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Payment service is slow",
            confidence=0.85,
            supporting_evidence_ids=[ev_owned.id, ev_foreign_id],
            contradicting_evidence_ids=[],
            status=HypothesisStatus.CONFIRMED,
            root_service="payment-service",
            cause_code="payment_latency_spike",
        )
        # Only ev_owned is in the incident's evidence list
        state = _make_state(hypotheses=[hyp], evidence=[ev_owned])
        assert can_generate_report(state) is False

    def test_report_accepts_all_owned_evidence(self) -> None:
        """Report can be generated when all supporting evidence is owned."""
        ev1 = _make_evidence()
        ev2 = _make_evidence()
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Payment service is slow",
            confidence=0.85,
            supporting_evidence_ids=[ev1.id, ev2.id],
            contradicting_evidence_ids=[],
            status=HypothesisStatus.CONFIRMED,
            root_service="payment-service",
            cause_code="payment_latency_spike",
        )
        state = _make_state(hypotheses=[hyp], evidence=[ev1, ev2])
        assert can_generate_report(state) is True


# ===================================================================
# REPORT FORMAT: uncertainty field
# ===================================================================


class TestReportUncertainty:
    """Report must contain uncertainty field."""

    def test_report_contains_uncertainty(self) -> None:
        """Generated report must include uncertainty field."""
        ev = _make_evidence()
        hyp = Hypothesis(
            id=str(uuid4()),
            description="Payment service is slow",
            confidence=0.85,
            supporting_evidence_ids=[ev.id],
            contradicting_evidence_ids=[],
            status=HypothesisStatus.CONFIRMED,
            root_service="payment-service",
            cause_code="payment_latency_spike",
        )
        state = _make_state(hypotheses=[hyp], evidence=[ev])
        report = generate_report(state)
        assert "uncertainty" in report
        # uncertainty = 1.0 - confidence = 1.0 - 0.85 = 0.15
        assert abs(report["uncertainty"] - 0.15) < 0.01


# ===================================================================
# CONTRADICTS ASSESSMENTS: evidence that contradicts a hypothesis
# ===================================================================


class TestContradictsAssessments:
    """Tests for contradicts=True assessments from evidence patterns."""

    def test_normal_latency_metrics_contradict_payment_latency_spike(self) -> None:
        """Normal latency metrics should contradict payment_latency_spike."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="query_metrics",
            content={
                "items": [
                    {
                        "service": "payment-service",
                        "name": "latency_p99",
                        "value": 50,
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        contradicts = [a for a in assessments if a.contradicts]
        assert len(contradicts) > 0
        latency_contradicts = [a for a in contradicts if a.root_cause == "payment_latency_spike"]
        assert len(latency_contradicts) > 0
        assert latency_contradicts[0].supports is False

    def test_low_error_rate_metrics_contradict_payment_degradation(self) -> None:
        """Low error rate metrics should contradict payment_service_degradation."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="query_metrics",
            content={
                "items": [
                    {
                        "service": "payment-service",
                        "name": "error_rate",
                        "value": 0.01,
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        contradicts = [a for a in assessments if a.contradicts]
        assert len(contradicts) > 0
        degradation_contradicts = [
            a for a in contradicts
            if a.root_cause == "payment_service_degradation"
        ]
        assert len(degradation_contradicts) > 0
        assert degradation_contradicts[0].supports is False

    def test_healthy_pool_metrics_contradict_database_connection_leak(self) -> None:
        """Healthy pool metrics should contradict database_connection_leak."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="query_metrics",
            content={
                "items": [
                    {
                        "service": "order-service",
                        "name": "db_pool_active",
                        "value": 2,
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        contradicts = [a for a in assessments if a.contradicts]
        assert len(contradicts) > 0
        db_contradicts = [a for a in contradicts if a.root_cause == "database_connection_leak"]
        assert len(db_contradicts) > 0
        assert db_contradicts[0].supports is False

    def test_normal_payment_log_contradicts_latency_spike(self) -> None:
        """Normal payment-service logs should contradict latency spike and degradation."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="search_logs",
            content={
                "items": [
                    {
                        "service": "payment-service",
                        "level": "INFO",
                        "message": "Payment completed successfully, normal latency",
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        contradicts = [a for a in assessments if a.contradicts]
        assert len(contradicts) >= 2  # Should contradict both latency_spike and degradation

    def test_stable_deployment_contradicts_bad_deployment(self) -> None:
        """Same version deployment should contradict bad_deployment."""
        from incidentlens_control_plane.agent.evidence_rules import assess_evidence

        evidence = _make_evidence(
            source_tool="list_recent_deployments",
            content={
                "items": [
                    {
                        "service": "payment-service",
                        "version": "same-as-previous",
                    }
                ]
            },
        )
        assessments = assess_evidence(evidence)
        contradicts = [a for a in assessments if a.contradicts]
        assert len(contradicts) > 0
        assert contradicts[0].root_cause == "bad_deployment"
        assert contradicts[0].supports is False
