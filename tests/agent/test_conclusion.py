"""Tests for conclusion readiness evaluation — TDD RED phase.

Tests verify:
  - evaluate_conclusion_readiness returns correct eligible cause codes
    and evidence IDs for each skill/policy combination
  - Empty evidence, error ToolResults, foreign incidents, invalid
    arguments, and direct contradictions are handled correctly
  - parse_proposal extracts valid RootCauseProposal from tool calls
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from incidentlens_contracts.models import Evidence

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_evidence(
    incident_id: str,
    source_tool: str,
    ev_id: str,
    content: dict[str, Any] | None = None,
) -> Evidence:
    return Evidence(
        id=ev_id,
        source_tool=source_tool,
        tool_call_id=f"call-{ev_id}",
        content={**(content or {}), "incident_id": incident_id},
    )


def material_evidence(incident_id: str, sources: set[str]) -> list[Evidence]:
    """Create one piece of evidence per source tool for the given incident."""
    evidence: list[Evidence] = []
    for i, source in enumerate(sorted(sources)):
        evidence.append(
            _make_evidence(
                incident_id=incident_id,
                source_tool=source,
                ev_id=f"ev-{source}-{i}",
                content={"data": [{"result": "ok"}]},
            )
        )
    return evidence


def load_test_policies() -> dict[str, Any]:
    """Create test evidence policies for all five skill types."""
    policies: dict[str, Any] = {}

    def _policy(
        skill_name: str,
        cause_code: str,
        required: list[str],
        min_independent: int,
        contradictions: list[str] | None = None,
    ) -> MagicMock:
        p = MagicMock()
        p.skill_name = skill_name
        p.cause_code = cause_code
        p.required_evidence_types = required
        p.minimum_independent_evidence = min_independent
        p.direct_contradictions = contradictions or []
        return p

    policies["payment_latency_spike"] = _policy(
        "downstream-timeout",
        "payment_latency_spike",
        ["search_logs", "get_slow_traces"],
        2,
    )
    policies["payment_service_degradation"] = _policy(
        "downstream-error",
        "payment_service_degradation",
        ["search_logs", "query_metrics"],
        2,
    )
    policies["database_connection_leak"] = _policy(
        "database-pool-exhaustion",
        "database_connection_leak",
        ["search_logs", "query_metrics"],
        2,
    )
    policies["network_partition"] = _policy(
        "dependency-unavailable",
        "network_partition",
        ["search_logs", "get_service_dependencies"],
        2,
    )
    policies["bad_deployment"] = _policy(
        "deployment-regression",
        "bad_deployment",
        ["list_recent_deployments", "query_metrics"],
        2,
    )
    return policies


# ---------------------------------------------------------------------------
# Five-skill parametrized readiness tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("skill_name", "cause_code", "sources"),
    [
        (
            "downstream-timeout",
            "payment_latency_spike",
            {"search_logs", "get_slow_traces"},
        ),
        (
            "downstream-error",
            "payment_service_degradation",
            {"search_logs", "query_metrics"},
        ),
        (
            "database-pool-exhaustion",
            "database_connection_leak",
            {"search_logs", "query_metrics"},
        ),
        (
            "dependency-unavailable",
            "network_partition",
            {"search_logs", "get_service_dependencies"},
        ),
        (
            "deployment-regression",
            "bad_deployment",
            {"list_recent_deployments", "query_metrics"},
        ),
    ],
)
def test_loaded_policy_becomes_ready_from_independent_material_sources(
    skill_name: str,
    cause_code: str,
    sources: set[str],
) -> None:
    """Each skill with its required evidence types should become ready."""
    from incidentlens_control_plane.agent.conclusion import (
        evaluate_conclusion_readiness,
    )

    readiness = evaluate_conclusion_readiness(
        incident_id="inc-1",
        loaded_skill_names=[skill_name],
        evidence=material_evidence("inc-1", sources),
        policies=load_test_policies(),
    )
    assert readiness.ready is True
    assert cause_code in readiness.eligible_cause_codes


# ---------------------------------------------------------------------------
# Empty / error / edge-case tests
# ---------------------------------------------------------------------------


def test_empty_evidence_returns_not_ready() -> None:
    """No evidence means not ready."""
    from incidentlens_control_plane.agent.conclusion import (
        evaluate_conclusion_readiness,
    )

    readiness = evaluate_conclusion_readiness(
        incident_id="inc-empty",
        loaded_skill_names=["downstream-timeout"],
        evidence=[],
        policies=load_test_policies(),
    )
    assert readiness.ready is False
    assert readiness.eligible_cause_codes == []
    assert readiness.eligible_evidence_ids == []


def test_empty_policies_returns_not_ready() -> None:
    """No policies means not ready."""
    from incidentlens_control_plane.agent.conclusion import (
        evaluate_conclusion_readiness,
    )

    readiness = evaluate_conclusion_readiness(
        incident_id="inc-no-pol",
        loaded_skill_names=["downstream-timeout"],
        evidence=material_evidence("inc-no-pol", {"search_logs", "get_slow_traces"}),
        policies={},
    )
    assert readiness.ready is False


def test_skill_not_loaded_returns_not_ready() -> None:
    """Evidence exists but the owning skill was not loaded."""
    from incidentlens_control_plane.agent.conclusion import (
        evaluate_conclusion_readiness,
    )

    readiness = evaluate_conclusion_readiness(
        incident_id="inc-skill",
        loaded_skill_names=[],  # no skill loaded
        evidence=material_evidence("inc-skill", {"search_logs", "get_slow_traces"}),
        policies=load_test_policies(),
    )
    assert readiness.ready is False


def test_foreign_incident_evidence_excluded() -> None:
    """Evidence from a different incident is excluded."""
    from incidentlens_control_plane.agent.conclusion import (
        evaluate_conclusion_readiness,
    )

    foreign_evidence = material_evidence("inc-other", {"search_logs", "get_slow_traces"})
    readiness = evaluate_conclusion_readiness(
        incident_id="inc-current",
        loaded_skill_names=["downstream-timeout"],
        evidence=foreign_evidence,
        policies=load_test_policies(),
    )
    assert readiness.ready is False


def test_insufficient_independent_sources_returns_not_ready() -> None:
    """Only one of two required independent sources is available."""
    from incidentlens_control_plane.agent.conclusion import (
        evaluate_conclusion_readiness,
    )

    readiness = evaluate_conclusion_readiness(
        incident_id="inc-partial",
        loaded_skill_names=["downstream-timeout"],
        evidence=material_evidence("inc-partial", {"search_logs"}),  # missing get_slow_traces
        policies=load_test_policies(),
    )
    assert readiness.ready is False


def test_direct_contradiction_prevents_readiness() -> None:
    """A contradicting evidence source prevents readiness for that cause code."""
    from incidentlens_control_plane.agent.conclusion import (
        evaluate_conclusion_readiness,
    )

    policies = load_test_policies()
    # Add a contradiction to payment_latency_spike
    policies["payment_latency_spike"].direct_contradictions = ["query_metrics"]

    evidence = material_evidence("inc-contra", {"search_logs", "get_slow_traces"})
    # Add contradicting metric evidence
    evidence.append(
        _make_evidence(
            incident_id="inc-contra",
            source_tool="query_metrics",
            ev_id="ev-query-metrics-0",
            content={"data": [{"value": 50}]},
        )
    )

    readiness = evaluate_conclusion_readiness(
        incident_id="inc-contra",
        loaded_skill_names=["downstream-timeout"],
        evidence=evidence,
        policies=policies,
    )
    # payment_latency_spike should NOT be eligible due to contradiction
    assert "payment_latency_spike" not in readiness.eligible_cause_codes


# ---------------------------------------------------------------------------
# parse_proposal tests
# ---------------------------------------------------------------------------


def test_parse_proposal_extracts_valid_proposal() -> None:
    """A valid RootCauseProposal tool call is extracted."""
    from incidentlens_control_plane.agent.conclusion import parse_proposal
    from incidentlens_control_plane.agent.types import RootCauseProposal

    tool_calls = [
        {
            "name": "search_logs",
            "args": {"incident_id": "inc-1"},
            "id": "call-1",
        },
        {
            "name": "RootCauseProposal",
            "args": {
                "root_service": "payment-service",
                "cause_code": "payment_latency_spike",
                "evidence_ids": ["ev-1", "ev-2"],
                "confidence": 0.9,
                "next_action": "finish",
            },
            "id": "call-2",
        },
    ]
    proposal = parse_proposal(tool_calls)
    assert isinstance(proposal, RootCauseProposal)
    assert proposal.root_service == "payment-service"
    assert proposal.cause_code == "payment_latency_spike"
    assert proposal.confidence == 0.9


def test_parse_proposal_returns_none_for_empty() -> None:
    """Empty tool calls returns None."""
    from incidentlens_control_plane.agent.conclusion import parse_proposal

    assert parse_proposal([]) is None


def test_parse_proposal_returns_none_for_invalid() -> None:
    """Invalid proposal args returns None."""
    from incidentlens_control_plane.agent.conclusion import parse_proposal

    tool_calls = [
        {
            "name": "RootCauseProposal",
            "args": {
                "root_service": "",  # empty, violates min_length
                "cause_code": "payment_latency_spike",
                "evidence_ids": ["ev-1"],
                "confidence": 0.9,
                "next_action": "finish",
            },
            "id": "call-1",
        },
    ]
    assert parse_proposal(tool_calls) is None


def test_parse_proposal_ignores_non_proposal_calls() -> None:
    """Non-RootCauseProposal tool calls are ignored."""
    from incidentlens_control_plane.agent.conclusion import parse_proposal

    tool_calls = [
        {"name": "search_logs", "args": {"incident_id": "inc-1"}, "id": "call-1"},
        {"name": "get_trace", "args": {"incident_id": "inc-1"}, "id": "call-2"},
    ]
    assert parse_proposal(tool_calls) is None
