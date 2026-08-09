import inspect

from incidentlens_control_plane.agent.factory import build_investigation_engine
from incidentlens_control_plane.agent.prompts import SYSTEM_PROMPT, build_agent_context


def test_engine_factory_has_no_legacy_case_dependencies() -> None:
    parameters = inspect.signature(build_investigation_engine).parameters
    assert "case_repository" not in parameters
    assert "memory" not in parameters


def test_agent_prompt_and_context_have_no_historical_cases() -> None:
    context = build_agent_context({"incident_id": "inc-1", "alert": {}})
    combined = f"{SYSTEM_PROMPT}\n{context}".lower()
    assert "historical case" not in combined
    assert "retrieved_cases" not in combined
