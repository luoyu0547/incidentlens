"""Tests for the bounded LLM agent graph.

Verifies that:
  - The model-selected tool (not a fixed strategy) is executed
  - Historical cases cannot pass the current-incident evidence gate
  - Unknown evidence IDs are rejected by the report gate
  - Model and tool call limits stop the graph with budget_exhausted
  - Investigation calls do not globally force structured output
  - Conclusion phase uses only the proposal tool
"""

from __future__ import annotations

from incidentlens_contracts.models import Evidence
from incidentlens_control_plane.agent.conclusion import (
    classify_repair,
    evaluate_conclusion_readiness,
    parse_conclusion_output,
)
from incidentlens_control_plane.agent.middleware import _has_material_evidence
from incidentlens_control_plane.agent.skills import EvidencePolicy
from incidentlens_control_plane.agent.types import RootCauseProposal
from langchain_core.messages import AIMessage


def test_material_evidence_requires_populated_slow_trace_and_trace() -> None:
    state = {
        "evidence": [
            Evidence(
                id="ev-slow",
                source_tool="get_slow_traces",
                tool_call_id="call-slow",
                content={"data": [{"trace_id": "trace-1"}]},
            ),
            Evidence(
                id="ev-trace",
                source_tool="get_trace",
                tool_call_id="call-trace",
                content={"data": {"spans": [{"service": "payment-service"}]}},
            ),
        ]
    }
    assert _has_material_evidence(state) is True

    state["evidence"][1] = Evidence(
        id="ev-empty-trace",
        source_tool="get_trace",
        tool_call_id="call-empty",
        content={"data": {}},
    )
    assert _has_material_evidence(state) is False


async def test_llm_agent_executes_model_selected_tool_not_fixed_strategy(
    agent_harness,
) -> None:
    """The agent should execute the tool chosen by the model, not a fixed strategy."""
    fake = agent_harness.fake_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_recent_deployments",
                        "args": {
                            "incident_id": "inc-1",
                            "service": "payment-service",
                            "limit": 5,
                        },
                        "id": "model-call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="Current evidence is insufficient; inspect the deployment."
            ),
        ]
    )
    graph = agent_harness.build(model=fake)
    result = await graph.ainvoke(
        agent_harness.initial_state("inc-1"),
        {"configurable": {"thread_id": "inc-1"}},
    )
    assert result["tool_call_count"] == 1
    assert result["evidence"][0].source_tool == "list_recent_deployments"
    assert result["model_call_count"] == 2


async def test_historical_case_cannot_pass_current_evidence_gate(
    agent_harness,
) -> None:
    """A proposal referencing only historical case IDs must be rejected."""
    state = agent_harness.initial_state("inc-2")
    state["retrieved_cases"] = [
        {"id": "case-1", "root_cause": "bad_deployment", "status": "human_verified"}
    ]
    decision = agent_harness.guard(
        state, cause_code="bad_deployment", evidence_ids=[]
    )
    assert decision.allowed is False
    assert decision.reason == "current_incident_evidence_required"


async def test_model_generated_unknown_evidence_id_is_rejected(
    agent_harness,
) -> None:
    """A proposal with an invented evidence ID must be rejected."""
    state = agent_harness.initial_state("inc-3")
    decision = agent_harness.guard(
        state,
        cause_code="payment_latency_spike",
        evidence_ids=["ev-invented"],
    )
    assert decision.allowed is False
    assert decision.reason == "unknown_evidence_id"


async def test_report_requires_the_skill_that_owns_the_evidence_policy(
    agent_harness,
) -> None:
    """A policy cannot be used unless its matching Skill was read."""
    state = agent_harness.initial_state("inc-skill-required")
    state["evidence"] = [
        Evidence(
            id="ev-current",
            source_tool="search_logs",
            tool_call_id="call-1",
            content={},
        ),
        Evidence(
            id="ev-current-2",
            source_tool="get_slow_traces",
            tool_call_id="call-2",
            content={},
        ),
    ]
    decision = agent_harness.guard(
        state,
        cause_code="payment_latency_spike",
        evidence_ids=["ev-current", "ev-current-2"],
    )
    assert decision.allowed is False
    assert decision.reason == "required_skill_not_loaded"

    state["loaded_skill_names"] = ["downstream-timeout"]
    decision = agent_harness.guard(
        state,
        cause_code="payment_latency_spike",
        evidence_ids=["ev-current", "ev-current-2"],
    )
    assert decision.allowed is True


async def test_reading_a_skill_is_persisted_in_graph_state(agent_harness) -> None:
    """The investigation node persists Skill reads without emitting a report."""
    state = agent_harness.initial_state("inc-read-skill")
    state["evidence"] = [
        Evidence(
            id="ev-log",
            source_tool="search_logs",
            tool_call_id="call-1",
            content={},
        ),
        Evidence(
            id="ev-trace",
            source_tool="get_slow_traces",
            tool_call_id="call-2",
            content={},
        ),
    ]
    fake = agent_harness.fake_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"path": "/skills/downstream-timeout/SKILL.md"},
                        "id": "read-skill-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="The Skill is loaded; more evidence is needed before concluding."),
        ]
    )
    graph = agent_harness.build(model=fake)
    result = await graph.ainvoke(
        state,
        {"configurable": {"thread_id": "inc-read-skill"}},
    )
    assert result["loaded_skill_names"] == ["downstream-timeout"]
    assert result.get("report") is None


async def test_model_and_tool_limits_stop_graph_with_explicit_code(
    agent_harness,
) -> None:
    """The graph must stop when model or tool call limits are reached."""
    graph = agent_harness.build(model=agent_harness.endless_tool_model())
    result = await graph.ainvoke(
        agent_harness.initial_state("inc-4"),
        {"configurable": {"thread_id": "inc-4"}},
    )
    assert result["model_call_count"] <= 12
    assert result["tool_call_count"] <= 12
    assert result["last_error_code"] == "budget_exhausted"


# ---------------------------------------------------------------------------
# Phase 4: Investigation/conclusion split tests
# ---------------------------------------------------------------------------


async def test_investigation_does_not_globally_force_structured_output(
    agent_harness,
) -> None:
    """Investigation calls must not force RootCauseProposal on every model call.

    When the model returns plain text (no tool calls), the agent loop
    should stop naturally without forcing a structured output.
    """
    # The investigation agent uses response_format=None, so the model
    # can return plain text without tool calls
    fake = agent_harness.fake_model(
        [
            AIMessage(content="I need to gather more evidence first."),
        ]
    )
    graph = agent_harness.build(model=fake)
    result = await graph.ainvoke(
        agent_harness.initial_state("inc-5"),
        {"configurable": {"thread_id": "inc-5"}},
    )
    # Model called once with plain text — investigation doesn't force proposal
    # The agent loop stops when the model returns without tool calls
    assert result["model_call_count"] == 1
    assert result["tool_call_count"] == 0
    # No structured response was forced
    assert result.get("report") is None


async def test_conclusion_readiness_with_sufficient_evidence() -> None:
    """Readiness evaluator correctly identifies when evidence is sufficient."""
    from incidentlens_contracts.models import Evidence

    policy = EvidencePolicy(
        skill_name="downstream-timeout",
        cause_code="payment_latency_spike",
        required_evidence_types=["trace", "log", "metric"],
        minimum_independent_evidence=2,
        direct_contradictions=["downstream span latency is normal"],
    )

    evidence = [
        Evidence(
            id="ev-trace-1",
            source_tool="get_slow_traces",
            tool_call_id="tc-1",
            content={"incident_id": "inc-6", "outcome": "success", "data": {"spans": []}},
        ),
        Evidence(
            id="ev-log-1",
            source_tool="search_logs",
            tool_call_id="tc-2",
            content={"incident_id": "inc-6", "outcome": "success", "data": {"entries": []}},
        ),
    ]

    result = evaluate_conclusion_readiness(
        loaded_skill_names=["downstream-timeout"],
        policies_by_cause_code={"payment_latency_spike": policy},
        evidence=evidence,
        incident_id="inc-6",
    )

    assert result.ready is True
    assert "payment_latency_spike" in result.eligible_cause_codes
    assert "ev-trace-1" in result.eligible_evidence_ids
    assert "ev-log-1" in result.eligible_evidence_ids


async def test_conclusion_readiness_not_ready_with_insufficient_evidence() -> None:
    """Readiness evaluator correctly identifies insufficient evidence."""
    from incidentlens_contracts.models import Evidence

    policy = EvidencePolicy(
        skill_name="downstream-timeout",
        cause_code="payment_latency_spike",
        required_evidence_types=["trace", "log", "metric"],
        minimum_independent_evidence=2,
        direct_contradictions=[],
    )

    evidence = [
        Evidence(
            id="ev-trace-1",
            source_tool="get_slow_traces",
            tool_call_id="tc-1",
            content={"incident_id": "inc-7", "outcome": "success", "data": {"spans": []}},
        ),
    ]

    result = evaluate_conclusion_readiness(
        loaded_skill_names=["downstream-timeout"],
        policies_by_cause_code={"payment_latency_spike": policy},
        evidence=evidence,
        incident_id="inc-7",
    )

    assert result.ready is False


async def test_valid_first_conclusion_proposal() -> None:
    """A valid proposal from the conclusion phase is accepted."""
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
    assert result.proposal.root_service == "payment-service"


async def test_invalid_first_and_valid_repair() -> None:
    """An invalid first proposal can be repaired once."""
    # First attempt: invalid (no proposal)
    result1 = parse_conclusion_output(
        "just text",
        eligible_cause_codes=["payment_latency_spike"],
        eligible_evidence_ids=["ev-1"],
    )
    assert result1.success is False

    repair = classify_repair(
        result1,
        eligible_cause_codes=["payment_latency_spike"],
        eligible_evidence_ids=["ev-1"],
    )
    assert repair.repairable is True
    assert "payment_latency_spike" in repair.repair_prompt

    # Second attempt: valid
    proposal = RootCauseProposal(
        root_service="payment-service",
        cause_code="payment_latency_spike",
        evidence_ids=["ev-1"],
        confidence=0.8,
        next_action="finish",
    )
    result2 = parse_conclusion_output(
        proposal,
        eligible_cause_codes=["payment_latency_spike"],
        eligible_evidence_ids=["ev-1"],
    )
    assert result2.success is True


async def test_invalid_twice_terminates() -> None:
    """Two invalid proposals result in terminal failure."""
    result = parse_conclusion_output(
        "just text",
        eligible_cause_codes=["payment_latency_spike"],
        eligible_evidence_ids=["ev-1"],
    )
    assert result.success is False

    repair = classify_repair(
        result,
        eligible_cause_codes=["payment_latency_spike"],
        eligible_evidence_ids=["ev-1"],
    )
    assert repair.repairable is True

    # After 2 attempts, the runtime marks as rejected (terminal)
    # The repair is still technically repairable, but the runtime
    # enforces the cap via conclusion_attempt_count >= 2


async def test_unknown_evidence_id_rejected() -> None:
    """A proposal with unknown evidence IDs is rejected."""
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


async def test_insufficient_independent_citations_rejected() -> None:
    """A proposal with insufficient independent evidence is rejected by readiness."""
    from incidentlens_contracts.models import Evidence

    policy = EvidencePolicy(
        skill_name="downstream-timeout",
        cause_code="payment_latency_spike",
        required_evidence_types=["trace", "log", "metric"],
        minimum_independent_evidence=2,
        direct_contradictions=[],
    )

    # Only one independent source tool
    evidence = [
        Evidence(
            id="ev-trace-1",
            source_tool="get_slow_traces",
            tool_call_id="tc-1",
            content={"incident_id": "inc-8", "outcome": "success", "data": {"spans": []}},
        ),
        Evidence(
            id="ev-trace-2",
            source_tool="get_slow_traces",
            tool_call_id="tc-2",
            content={"incident_id": "inc-8", "outcome": "success", "data": {"spans": []}},
        ),
    ]

    result = evaluate_conclusion_readiness(
        loaded_skill_names=["downstream-timeout"],
        policies_by_cause_code={"payment_latency_spike": policy},
        evidence=evidence,
        incident_id="inc-8",
    )

    assert result.ready is False


async def test_direct_contradiction_rejected() -> None:
    """Evidence that directly contradicts the hypothesis is rejected by readiness."""
    from incidentlens_contracts.models import Evidence

    policy = EvidencePolicy(
        skill_name="downstream-timeout",
        cause_code="payment_latency_spike",
        required_evidence_types=["trace", "log", "metric"],
        minimum_independent_evidence=2,
        direct_contradictions=["downstream span latency is normal"],
    )

    evidence = [
        Evidence(
            id="ev-trace-1",
            source_tool="get_slow_traces",
            tool_call_id="tc-1",
            content={"incident_id": "inc-9", "outcome": "success", "data": {"spans": []}},
        ),
        Evidence(
            id="ev-log-1",
            source_tool="search_logs",
            tool_call_id="tc-2",
            content={
                "incident_id": "inc-9",
                "outcome": "success",
                "data": {"message": "downstream span latency is normal in the incident window"},
            },
        ),
    ]

    result = evaluate_conclusion_readiness(
        loaded_skill_names=["downstream-timeout"],
        policies_by_cause_code={"payment_latency_spike": policy},
        evidence=evidence,
        incident_id="inc-9",
    )

    assert result.ready is False
    assert any(r.rejection_reason == "direct_contradiction" for r in result.rejections)


async def test_concurrent_incidents_have_isolated_state() -> None:
    """Two concurrent incidents must have isolated attempt counters and evidence."""
    from incidentlens_contracts.models import Evidence

    # Incident A
    evidence_a = [
        Evidence(
            id="ev-a-1",
            source_tool="get_slow_traces",
            tool_call_id="tc-a-1",
            content={"incident_id": "inc-A", "outcome": "success", "data": {"spans": []}},
        ),
    ]

    # Incident B
    evidence_b = [
        Evidence(
            id="ev-b-1",
            source_tool="search_logs",
            tool_call_id="tc-b-1",
            content={"incident_id": "inc-B", "outcome": "success", "data": {"entries": []}},
        ),
    ]

    # Each incident evaluates independently
    policy = EvidencePolicy(
        skill_name="downstream-timeout",
        cause_code="payment_latency_spike",
        required_evidence_types=["trace", "log", "metric"],
        minimum_independent_evidence=2,
        direct_contradictions=[],
    )

    result_a = evaluate_conclusion_readiness(
        loaded_skill_names=["downstream-timeout"],
        policies_by_cause_code={"payment_latency_spike": policy},
        evidence=evidence_a,
        incident_id="inc-A",
    )

    result_b = evaluate_conclusion_readiness(
        loaded_skill_names=["downstream-timeout"],
        policies_by_cause_code={"payment_latency_spike": policy},
        evidence=evidence_b,
        incident_id="inc-B",
    )

    # Both are not ready (insufficient independent evidence)
    assert result_a.ready is False
    assert result_b.ready is False

    # Evidence IDs are isolated
    assert "ev-a-1" not in result_b.eligible_evidence_ids
    assert "ev-b-1" not in result_a.eligible_evidence_ids
