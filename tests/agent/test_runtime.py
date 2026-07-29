from pathlib import Path
from types import SimpleNamespace

import pytest
from incidentlens_control_plane.agent.factory import build_investigation_engine
from incidentlens_control_plane.agent.runtime import LLMInvestigationEngine
from incidentlens_control_plane.llm.config import RuntimeMode
from incidentlens_control_plane.llm.registry import ModelIdentity


def test_llm_mode_requires_registry_and_never_auto_selects_baseline(
    telemetry_repo,
    toolkit,
    investigation_audit_store,
) -> None:
    with pytest.raises(ValueError, match="ModelRegistry"):
        build_investigation_engine(
            mode=RuntimeMode.LLM_AGENT,
            model_registry=None,
            telemetry_repo=telemetry_repo,
            toolkit=toolkit,
            case_repository=None,
            audit_store=investigation_audit_store,
            checkpointer=object(),
            skill_runtime=object(),
        )


def test_baseline_mode_does_not_construct_model_registry(
    telemetry_repo,
    toolkit,
    investigation_audit_store,
) -> None:
    engine = build_investigation_engine(
        mode=RuntimeMode.DETERMINISTIC_BASELINE,
        model_registry=None,
        telemetry_repo=telemetry_repo,
        toolkit=toolkit,
        case_repository=None,
        audit_store=investigation_audit_store,
        checkpointer=None,
        skill_runtime=None,
    )
    assert engine.mode is RuntimeMode.DETERMINISTIC_BASELINE


async def test_terminal_incident_does_not_restart(investigation_audit_store) -> None:
    terminal = {
        "messages": [],
        "incident_id": "inc-terminal",
        "status": "needs_more_evidence",
        "phase": "finished",
        "alert": {"service": "order-service"},
        "current_round": 2,
        "max_rounds": 8,
        "hypotheses": [],
        "evidence": [],
        "retrieved_cases": [],
        "loaded_skill_names": [],
        "model_profile": "test",
        "model_call_count": 1,
        "tool_call_count": 0,
        "fallback_used": False,
        "report": None,
    }

    class StubGraph:
        invocations = 0

        async def aget_state(self, config):
            return SimpleNamespace(values=terminal)

        async def ainvoke(self, *args, **kwargs):
            self.invocations += 1
            raise AssertionError("terminal thread was restarted")

    graph = StubGraph()
    engine = LLMInvestigationEngine(
        graph=graph,
        audit_store=investigation_audit_store,
        model_identity=ModelIdentity("test", "test-model", "example.test"),
        case_repository=None,
        total_timeout_seconds=1200,
    )
    resumed = await engine.resume("inc-terminal")
    assert resumed.status.value == "needs_more_evidence"
    assert graph.invocations == 0


async def test_run_round_supplies_continuation_message_for_completed_turn(
    investigation_audit_store,
) -> None:
    active = {
        "messages": [],
        "incident_id": "inc-active",
        "status": "investigating",
        "phase": "agent_loop",
        "alert": {"service": "payment-service"},
        "current_round": 0,
        "max_rounds": 8,
        "hypotheses": [],
        "evidence": [],
        "retrieved_cases": [],
        "loaded_skill_names": ["downstream-timeout"],
        "model_profile": "test",
        "model_call_count": 2,
        "tool_call_count": 0,
        "fallback_used": False,
        "report": None,
    }

    class StubGraph:
        invocation_input = None

        async def aget_state(self, config):
            return SimpleNamespace(values=active)

        async def ainvoke(self, input, **kwargs):
            self.invocation_input = input

    graph = StubGraph()
    engine = LLMInvestigationEngine(
        graph=graph,
        audit_store=investigation_audit_store,
        model_identity=ModelIdentity("test", "test-model", "example.test"),
        case_repository=None,
    )

    await engine.run_round("inc-active")

    assert graph.invocation_input is not None
    message = graph.invocation_input["messages"][0]
    assert "Continue the current investigation" in message.content


def test_llm_engine_source_has_no_fixed_tool_strategy() -> None:
    source = Path(
        "apps/control-plane/src/incidentlens_control_plane/agent/runtime.py"
    ).read_text(encoding="utf-8")
    assert "_TOOL_STRATEGY" not in source
