"""Tests for the bounded LLM agent graph.

Verifies that:
  - The model-selected tool (not a fixed strategy) is executed
  - Historical cases cannot pass the current-incident evidence gate
  - Unknown evidence IDs are rejected by the report gate
  - Model and tool call limits stop the graph with budget_exhausted
"""

from __future__ import annotations

from incidentlens_contracts.models import Evidence
from incidentlens_control_plane.agent.middleware import _has_material_evidence
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


async def test_accepted_proposal_is_projected_to_a_report(agent_harness) -> None:
    """A valid structured response must become the API-visible report state."""
    state = agent_harness.initial_state("inc-report")
    state["loaded_skill_names"] = ["downstream-timeout"]
    state["evidence"] = [
        Evidence(
            id="ev-log",
            source_tool="search_logs",
            tool_call_id="call-1",
            content={"data": {"message": "timeout"}},
        ),
        Evidence(
            id="ev-trace",
            source_tool="get_slow_traces",
            tool_call_id="call-2",
            content={"data": {"duration_ms": 6000}},
        ),
    ]
    fake = agent_harness.fake_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "RootCauseProposal",
                        "args": {
                            "root_service": "payment-service",
                            "cause_code": "payment_latency_spike",
                            "evidence_ids": ["ev-log", "ev-trace"],
                            "confidence": 0.9,
                            "next_action": "finish",
                        },
                        "id": "proposal-1",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    graph = agent_harness.build(model=fake)
    result = await graph.ainvoke(
        state,
        {"configurable": {"thread_id": "inc-report"}},
    )
    assert result["status"] == "report_ready"
    assert result["report"]["root_service"] == "payment-service"
    assert result["report"]["evidence_ids"] == ["ev-log", "ev-trace"]


async def test_reading_a_skill_unblocks_its_guarded_report(agent_harness) -> None:
    """The read_file tool must persist the loaded Skill in graph state."""
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
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "RootCauseProposal",
                        "args": {
                            "root_service": "payment-service",
                            "cause_code": "payment_latency_spike",
                            "evidence_ids": ["ev-log", "ev-trace"],
                            "confidence": 0.9,
                            "next_action": "finish",
                        },
                        "id": "proposal-after-skill",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    )
    graph = agent_harness.build(model=fake)
    result = await graph.ainvoke(
        state,
        {"configurable": {"thread_id": "inc-read-skill"}},
    )
    assert result["loaded_skill_names"] == ["downstream-timeout"]
    assert result["status"] == "report_ready"


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
