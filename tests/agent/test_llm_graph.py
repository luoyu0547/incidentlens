"""Tests for the bounded LLM agent graph.

Verifies that:
  - The model-selected tool (not a fixed strategy) is executed
  - Historical cases cannot pass the current-incident evidence gate
  - Unknown evidence IDs are rejected by the report gate
  - Model and tool call limits stop the graph with budget_exhausted
"""

from __future__ import annotations

from langchain_core.messages import AIMessage


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
