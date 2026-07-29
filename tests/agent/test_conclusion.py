"""Tests for conclusion readiness models.

Verifies:
  - Material evidence classification
  - Policy eligibility for all 5 Skill policies
  - Conclusion readiness evaluation
  - Proposal parsing and validation
  - Repair classification

All tests are provider-neutral: no scenario names or expected root causes
are encoded in the readiness implementation.
"""

from __future__ import annotations

import pytest
from incidentlens_contracts.models import Evidence

from incidentlens_control_plane.agent.conclusion import (
    ProposalResult,
    classify_repair,
    evaluate_conclusion_readiness,
    evaluate_policy_eligibility,
    is_material_evidence,
    parse_conclusion_output,
)
from incidentlens_control_plane.agent.skills import EvidencePolicy
from incidentlens_control_plane.agent.types import RootCauseProposal


# ---------------------------------------------------------------------------
# EvidencePolicy fixtures for all 5 skills
# ---------------------------------------------------------------------------

POLICIES = {
    "payment_latency_spike": EvidencePolicy(
        skill_name="downstream-timeout",
        cause_code="payment_latency_spike",
        required_evidence_types=["trace", "log", "metric"],
        minimum_independent_evidence=2,
        direct_contradictions=["downstream span latency is normal in the incident window"],
    ),
    "payment_service_degradation": EvidencePolicy(
        skill_name="downstream-error",
        cause_code="payment_service_degradation",
        required_evidence_types=["trace", "log", "metric"],
        minimum_independent_evidence=2,
        direct_contradictions=["downstream success and error rates remain at baseline"],
    ),
    "database_connection_leak": EvidencePolicy(
        skill_name="database-pool-exhaustion",
        cause_code="database_connection_leak",
        required_evidence_types=["log", "metric", "trace"],
        minimum_independent_evidence=2,
        direct_contradictions=["available pool capacity remains healthy during failed requests"],
    ),
    "network_partition": EvidencePolicy(
        skill_name="dependency-unavailable",
        cause_code="network_partition",
        required_evidence_types=["log", "trace", "metric"],
        minimum_independent_evidence=2,
        direct_contradictions=["successful dependency calls continue through the same incident window"],
    ),
    "bad_deployment": EvidencePolicy(
        skill_name="deployment-regression",
        cause_code="bad_deployment",
        required_evidence_types=["deployment", "log", "trace"],
        minimum_independent_evidence=2,
        direct_contradictions=["the same failure predates the candidate deployment"],
    ),
}


def _make_evidence(
    ev_id: str,
    source_tool: str,
    incident_id: str = "inc-1",
    data: dict | None = None,
    outcome: str = "success",
) -> Evidence:
    """Helper to create an Evidence object."""
    content: dict = {"incident_id": incident_id, "outcome": outcome}
    if data is not None:
        content["data"] = data
    return Evidence(
        id=ev_id,
        source_tool=source_tool,
        tool_call_id=f"tc-{ev_id}",
        content=content,
    )


# ---------------------------------------------------------------------------
# Material evidence classification
# ---------------------------------------------------------------------------


class TestMaterialEvidenceClassification:
    """Tests for is_material_evidence."""

    def test_material_evidence_with_data(self) -> None:
        ev = _make_evidence("ev-1", "search_logs", data={"count": 5})
        assert is_material_evidence(ev, "inc-1") is True

    def test_material_evidence_with_count(self) -> None:
        ev = _make_evidence("ev-1", "query_metrics")
        ev.content["count"] = 3
        assert is_material_evidence(ev, "inc-1") is True

    def test_not_material_wrong_incident(self) -> None:
        ev = _make_evidence("ev-1", "search_logs", incident_id="inc-other", data={"x": 1})
        assert is_material_evidence(ev, "inc-1") is False

    def test_not_material_invalid_arguments(self) -> None:
        ev = _make_evidence("ev-1", "search_logs", outcome="invalid_arguments")
        assert is_material_evidence(ev, "inc-1") is False

    def test_not_material_error_outcome(self) -> None:
        ev = _make_evidence("ev-1", "search_logs", outcome="error")
        assert is_material_evidence(ev, "inc-1") is False

    def test_not_material_empty_content(self) -> None:
        ev = _make_evidence("ev-1", "search_logs")
        # No data or count in content
        assert is_material_evidence(ev, "inc-1") is False


# ---------------------------------------------------------------------------
# Policy eligibility (parameterized over all 5 skills)
# ---------------------------------------------------------------------------


class TestPolicyEligibility:
    """Tests for evaluate_policy_eligibility parameterized over all 5 skills."""

    @pytest.mark.parametrize(
        "cause_code,policy",
        list(POLICIES.items()),
        ids=list(POLICIES.keys()),
    )
    def test_eligible_with_sufficient_independent_evidence(
        self, cause_code: str, policy: EvidencePolicy
    ) -> None:
        evidence = [
            _make_evidence("ev-trace", "get_slow_traces", data={"spans": []}),
            _make_evidence("ev-log", "search_logs", data={"entries": []}),
        ]
        result = evaluate_policy_eligibility(policy, evidence)
        assert result.eligible is True
        assert result.cause_code == cause_code
        assert len(result.supporting_evidence_ids) == 2

    @pytest.mark.parametrize(
        "cause_code,policy",
        list(POLICIES.items()),
        ids=list(POLICIES.keys()),
    )
    def test_not_eligible_insufficient_evidence(
        self, cause_code: str, policy: EvidencePolicy
    ) -> None:
        evidence = [
            _make_evidence("ev-trace", "get_slow_traces", data={"spans": []}),
        ]
        result = evaluate_policy_eligibility(policy, evidence)
        assert result.eligible is False
        assert result.rejection_reason == "insufficient_independent_evidence"

    @pytest.mark.parametrize(
        "cause_code,policy",
        list(POLICIES.items()),
        ids=list(POLICIES.keys()),
    )
    def test_not_eligible_direct_contradiction(
        self, cause_code: str, policy: EvidencePolicy
    ) -> None:
        # Provide enough evidence types but with a contradiction
        contradiction_text = policy.direct_contradictions[0]
        evidence = [
            _make_evidence("ev-trace", "get_slow_traces", data={"spans": []}),
            _make_evidence("ev-log", "search_logs", data={"message": contradiction_text}),
        ]
        result = evaluate_policy_eligibility(policy, evidence)
        assert result.eligible is False
        assert result.rejection_reason == "direct_contradiction"

    @pytest.mark.parametrize(
        "cause_code,policy",
        list(POLICIES.items()),
        ids=list(POLICIES.keys()),
    )
    def test_empty_evidence_not_eligible(
        self, cause_code: str, policy: EvidencePolicy
    ) -> None:
        result = evaluate_policy_eligibility(policy, [])
        assert result.eligible is False
        assert result.rejection_reason == "insufficient_independent_evidence"


# ---------------------------------------------------------------------------
# Conclusion readiness evaluation
# ---------------------------------------------------------------------------


class TestConclusionReadiness:
    """Tests for evaluate_conclusion_readiness."""

    def test_ready_when_policy_eligible(self) -> None:
        evidence = [
            _make_evidence("ev-1", "get_slow_traces", data={"spans": []}),
            _make_evidence("ev-2", "search_logs", data={"entries": []}),
        ]
        result = evaluate_conclusion_readiness(
            loaded_skill_names=["downstream-timeout"],
            policies_by_cause_code=POLICIES,
            evidence=evidence,
            incident_id="inc-1",
        )
        assert result.ready is True
        assert "payment_latency_spike" in result.eligible_cause_codes

    def test_not_ready_when_no_loaded_skills(self) -> None:
        evidence = [
            _make_evidence("ev-1", "get_slow_traces", data={"spans": []}),
            _make_evidence("ev-2", "search_logs", data={"entries": []}),
        ]
        result = evaluate_conclusion_readiness(
            loaded_skill_names=[],
            policies_by_cause_code=POLICIES,
            evidence=evidence,
            incident_id="inc-1",
        )
        assert result.ready is False

    def test_not_ready_when_insufficient_evidence(self) -> None:
        evidence = [
            _make_evidence("ev-1", "get_slow_traces", data={"spans": []}),
        ]
        result = evaluate_conclusion_readiness(
            loaded_skill_names=["downstream-timeout"],
            policies_by_cause_code=POLICIES,
            evidence=evidence,
            incident_id="inc-1",
        )
        assert result.ready is False

    def test_not_ready_when_no_material_evidence(self) -> None:
        evidence = [
            _make_evidence("ev-1", "search_logs", outcome="error"),
        ]
        result = evaluate_conclusion_readiness(
            loaded_skill_names=["downstream-timeout"],
            policies_by_cause_code=POLICIES,
            evidence=evidence,
            incident_id="inc-1",
        )
        assert result.ready is False

    def test_multiple_eligible_policies(self) -> None:
        evidence = [
            _make_evidence("ev-1", "get_slow_traces", data={"spans": []}),
            _make_evidence("ev-2", "search_logs", data={"entries": []}),
            _make_evidence("ev-3", "query_metrics", data={"metrics": []}),
        ]
        result = evaluate_conclusion_readiness(
            loaded_skill_names=["downstream-timeout", "downstream-error"],
            policies_by_cause_code=POLICIES,
            evidence=evidence,
            incident_id="inc-1",
        )
        assert result.ready is True
        assert len(result.eligible_cause_codes) >= 2


# ---------------------------------------------------------------------------
# Proposal parsing
# ---------------------------------------------------------------------------


class TestProposalParsing:
    """Tests for parse_conclusion_output."""

    def test_parse_valid_proposal(self) -> None:
        proposal = RootCauseProposal(
            root_service="payment-service",
            cause_code="payment_latency_spike",
            evidence_ids=["ev-1", "ev-2"],
            confidence=0.85,
            next_action="finish",
        )
        result = parse_conclusion_output(
            proposal,
            eligible_cause_codes=["payment_latency_spike"],
            eligible_evidence_ids=["ev-1", "ev-2"],
        )
        assert result.success is True
        assert result.proposal is not None
        assert result.proposal.cause_code == "payment_latency_spike"

    def test_parse_no_proposal(self) -> None:
        result = parse_conclusion_output(
            "just text",
            eligible_cause_codes=["payment_latency_spike"],
            eligible_evidence_ids=["ev-1"],
        )
        assert result.success is False
        assert result.error_code == "no_proposal_tool_call"

    def test_parse_unknown_cause_code(self) -> None:
        proposal = RootCauseProposal(
            root_service="payment-service",
            cause_code="unknown_code",
            evidence_ids=["ev-1"],
            confidence=0.8,
            next_action="finish",
        )
        result = parse_conclusion_output(
            proposal,
            eligible_cause_codes=["payment_latency_spike"],
            eligible_evidence_ids=["ev-1"],
        )
        assert result.success is False
        assert result.error_code == "unknown_cause_code"

    def test_parse_unknown_evidence_id(self) -> None:
        proposal = RootCauseProposal(
            root_service="payment-service",
            cause_code="payment_latency_spike",
            evidence_ids=["ev-invented"],
            confidence=0.8,
            next_action="finish",
        )
        result = parse_conclusion_output(
            proposal,
            eligible_cause_codes=["payment_latency_spike"],
            eligible_evidence_ids=["ev-1", "ev-2"],
        )
        assert result.success is False
        assert result.error_code == "unknown_evidence_id"

    def test_parse_dict_proposal(self) -> None:
        raw = {
            "root_service": "payment-service",
            "cause_code": "payment_latency_spike",
            "evidence_ids": ["ev-1"],
            "confidence": 0.9,
            "next_action": "finish",
        }
        result = parse_conclusion_output(
            raw,
            eligible_cause_codes=["payment_latency_spike"],
            eligible_evidence_ids=["ev-1"],
        )
        assert result.success is True

    def test_parse_from_tool_calls(self) -> None:
        raw = {
            "tool_calls": [
                {
                    "name": "root_cause_proposal",
                    "args": {
                        "root_service": "payment-service",
                        "cause_code": "payment_latency_spike",
                        "evidence_ids": ["ev-1"],
                        "confidence": 0.9,
                        "next_action": "finish",
                    },
                }
            ]
        }
        result = parse_conclusion_output(
            raw,
            eligible_cause_codes=["payment_latency_spike"],
            eligible_evidence_ids=["ev-1"],
        )
        assert result.success is True

    def test_parse_needs_more_evidence(self) -> None:
        proposal = RootCauseProposal(
            root_service="payment-service",
            cause_code="payment_latency_spike",
            evidence_ids=["ev-1"],
            confidence=0.5,
            next_action="needs_more_evidence",
        )
        result = parse_conclusion_output(
            proposal,
            eligible_cause_codes=["payment_latency_spike"],
            eligible_evidence_ids=["ev-1"],
        )
        assert result.success is True
        assert result.proposal.next_action == "needs_more_evidence"


# ---------------------------------------------------------------------------
# Repair classification
# ---------------------------------------------------------------------------


class TestRepairClassification:
    """Tests for classify_repair."""

    def test_no_repair_needed_for_success(self) -> None:
        result = classify_repair(
            ProposalResult(success=True),
            eligible_cause_codes=["payment_latency_spike"],
            eligible_evidence_ids=["ev-1"],
        )
        assert result.repairable is False

    def test_repairable_no_proposal(self) -> None:
        result = classify_repair(
            ProposalResult(
                success=False,
                error_code="no_proposal_tool_call",
                error_detail="No tool call",
            ),
            eligible_cause_codes=["payment_latency_spike"],
            eligible_evidence_ids=["ev-1"],
        )
        assert result.repairable is True
        assert result.error_code == "no_proposal_tool_call"
        assert "payment_latency_spike" in result.repair_prompt

    def test_repairable_unknown_evidence_id(self) -> None:
        result = classify_repair(
            ProposalResult(
                success=False,
                error_code="unknown_evidence_id",
                error_detail="ev-invented not eligible",
            ),
            eligible_cause_codes=["payment_latency_spike"],
            eligible_evidence_ids=["ev-1", "ev-2"],
        )
        assert result.repairable is True
        assert "ev-1" in result.repair_prompt
        assert "ev-2" in result.repair_prompt

    def test_repairable_unknown_cause_code(self) -> None:
        result = classify_repair(
            ProposalResult(
                success=False,
                error_code="unknown_cause_code",
                error_detail="unknown_code not eligible",
            ),
            eligible_cause_codes=["payment_latency_spike"],
            eligible_evidence_ids=["ev-1"],
        )
        assert result.repairable is True
        assert "payment_latency_spike" in result.repair_prompt
