from pathlib import Path

from incidentlens_control_plane.agent.checkpoint import AgentCheckpointRuntime
from incidentlens_control_plane.agent.projection import project_investigation_state
from incidentlens_control_plane.agent.types import IncidentAgentState
from langgraph.graph import END, START, StateGraph


def test_projection_validates_domain_state() -> None:
    projected = project_investigation_state(
        {
            "incident_id": "inc-1",
            "status": "investigating",
            "current_round": 1,
            "max_rounds": 8,
            "alert": {"service": "order-service"},
            "hypotheses": [],
            "evidence": [],
            "report": None,
            "phase": "agent_loop",
            "retrieved_cases": [],
            "loaded_skill_names": ["downstream-timeout"],
            "model_profile": "deepseek",
            "model_call_count": 1,
            "tool_call_count": 0,
            "fallback_used": False,
        }
    )
    assert projected.incident_id == "inc-1"
    assert projected.status.value == "investigating"


async def test_sqlite_checkpoint_uses_incident_id_as_thread_id(tmp_path: Path) -> None:
    async with AgentCheckpointRuntime(tmp_path / "agent.db") as checkpoints:
        builder = StateGraph(IncidentAgentState)
        builder.add_node("advance", lambda state: {"current_round": 1})
        builder.add_edge(START, "advance")
        builder.add_edge("advance", END)
        graph = builder.compile(checkpointer=checkpoints.saver)
        config = checkpoints.config_for("inc-1")
        await graph.ainvoke(
            {
                "messages": [],
                "incident_id": "inc-1",
                "current_round": 0,
            },
            config,
        )
        saved = await graph.aget_state(config)
        assert saved.config["configurable"]["thread_id"] == "inc-1"
        assert saved.values["current_round"] == 1
