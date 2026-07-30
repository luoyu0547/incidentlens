"""Shared fixtures for agent graph tests.

Provides AgentHarness which owns the real SQLite checkpoint runtime,
real tool adapters, real SkillRuntime, and an injected ScriptedChatModel.

Also provides RecoveryHarness for testing interrupt/resume, timeout,
and checkpoint corruption scenarios.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from incidentlens_control_plane.agent.checkpoint import AgentCheckpointRuntime
from incidentlens_control_plane.agent.graph import build_investigation_agent
from incidentlens_control_plane.agent.middleware import can_generate_guarded_report
from incidentlens_control_plane.agent.skills import SkillRuntime
from incidentlens_control_plane.agent.state import (
    InvestigationAuditStore,
)
from incidentlens_control_plane.agent.tool_adapter import EvidenceRecorder, build_agent_tools
from incidentlens_control_plane.agent.types import (
    IncidentAgentState,
    RootCauseProposal,
)
from incidentlens_control_plane.llm.registry import ModelIdentity
from incidentlens_control_plane.tools.query import ReadOnlyToolkit
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.types import interrupt

# Add project root to sys.path so that tests.support is importable
_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from tests.support.fake_chat_model import ScriptedChatModel  # noqa: E402

# ---------------------------------------------------------------------------
# InterruptibleScriptedChatModel
# ---------------------------------------------------------------------------


class InterruptibleScriptedChatModel(ScriptedChatModel):
    """A scripted model that can interrupt, timeout, or emit tool calls.

    Behaviour on each call:
      - 1st call: emits a ``search_logs`` tool call.
      - 2nd call: one of three things depending on flags:
        * ``interrupt_before_second_model_call`` is True  -> calls ``interrupt()``
        * ``timeout_before_second_model_call`` is True
          -> raises ``TimeoutError``
        * otherwise
          -> emits a valid ``RootCauseProposal`` tool call
      - 3rd+ call: emits a valid ``RootCauseProposal`` tool call.
    """

    interrupt_before_second_model_call: bool = False
    timeout_before_second_model_call: bool = False
    invocations: int = 0

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        from langchain_core.outputs import ChatGeneration, ChatResult

        self.invocations += 1

        if self.invocations == 1:
            # First call: emit search_logs tool call
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_logs",
                        "args": {
                            "incident_id": "test-incident",
                            "service": "order-service",
                            "keyword": "timeout",
                            "limit": 10,
                        },
                        "id": "tool-call-1",
                        "type": "tool_call",
                    }
                ],
            )
        elif self.invocations == 2:
            if self.interrupt_before_second_model_call:
                interrupt({"reason": "test_after_tool"})
            elif self.timeout_before_second_model_call:
                raise TimeoutError("model timeout")
            else:
                # Emit a valid RootCauseProposal tool call
                message = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "root_cause_proposal",
                            "args": {
                                "root_service": "order-service",
                                "cause_code": "timeout_cascade",
                                "evidence_ids": ["ev-test-evidence"],
                                "confidence": 0.85,
                                "next_action": "finish",
                            },
                            "id": "proposal-call-1",
                            "type": "tool_call",
                        }
                    ],
                )
        else:
            # 3rd+ call: emit a valid RootCauseProposal tool call
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "root_cause_proposal",
                        "args": {
                            "root_service": "order-service",
                            "cause_code": "timeout_cascade",
                            "evidence_ids": ["ev-test-evidence"],
                            "confidence": 0.85,
                            "next_action": "finish",
                        },
                        "id": f"proposal-call-{self.invocations}",
                        "type": "tool_call",
                    }
                ],
            )

        return ChatResult(generations=[ChatGeneration(message=message)])


# ---------------------------------------------------------------------------
# CountingToolkit
# ---------------------------------------------------------------------------


class CountingToolkit:
    """Wraps a real ReadOnlyToolkit and counts tool executions.

    Each method delegates to the underlying toolkit and increments a
    per-tool counter before delegation.  ``total_count`` returns the
    sum of all tool invocations across all tools.
    """

    def __init__(self, delegate: ReadOnlyToolkit) -> None:
        self._delegate = delegate
        self._counts: dict[str, int] = {}

    def _increment(self, tool_name: str) -> None:
        self._counts[tool_name] = self._counts.get(tool_name, 0) + 1

    def count(self, tool_name: str) -> int:
        """Return the invocation count for a specific tool."""
        return self._counts.get(tool_name, 0)

    @property
    def total_count(self) -> int:
        """Return the total number of tool invocations across all tools."""
        return sum(self._counts.values())

    async def search_logs(self, **kwargs: Any) -> Any:
        self._increment("search_logs")
        return await self._delegate.search_logs(**kwargs)

    async def query_metrics(self, **kwargs: Any) -> Any:
        self._increment("query_metrics")
        return await self._delegate.query_metrics(**kwargs)

    async def get_slow_traces(self, **kwargs: Any) -> Any:
        self._increment("get_slow_traces")
        return await self._delegate.get_slow_traces(**kwargs)

    async def get_trace(self, **kwargs: Any) -> Any:
        self._increment("get_trace")
        return await self._delegate.get_trace(**kwargs)

    async def get_service_dependencies(self, **kwargs: Any) -> Any:
        self._increment("get_service_dependencies")
        return await self._delegate.get_service_dependencies(**kwargs)

    async def list_recent_deployments(self, **kwargs: Any) -> Any:
        self._increment("list_recent_deployments")
        return await self._delegate.list_recent_deployments(**kwargs)

    async def get_runbook(self, **kwargs: Any) -> Any:
        self._increment("get_runbook")
        return await self._delegate.get_runbook(**kwargs)


# ---------------------------------------------------------------------------
# RecoveryRunResult
# ---------------------------------------------------------------------------


@dataclass
class RecoveryRunResult:
    """Result of a recovery harness run, carrying the projected state and
    the number of tool executions observed so far."""

    state: Any  # InvestigationState
    tool_executions: int


# ---------------------------------------------------------------------------
# RecoveryHarness
# ---------------------------------------------------------------------------


class RecoveryHarness:
    """Test harness for interrupt/resume, timeout, and checkpoint corruption.

    Owns a real ``AgentCheckpointRuntime``, ``LLMInvestigationEngine``,
    ``InterruptibleScriptedChatModel``, and ``CountingToolkit``.
    """

    def __init__(
        self,
        *,
        engine: Any,
        counted_toolkit: CountingToolkit,
        checkpoint_runtime: AgentCheckpointRuntime,
        scripted_model: InterruptibleScriptedChatModel,
    ) -> None:
        self.engine = engine
        self._toolkit = counted_toolkit
        self._checkpoints = checkpoint_runtime
        self._model = scripted_model

    @property
    def model_invocations(self) -> int:
        return self._model.invocations

    async def run_until_after_tool(
        self,
        *,
        incident_id: str,
        tool_name: str,
    ) -> RecoveryRunResult:
        """Start an investigation and let it run until after the first tool
        call, then pause via interrupt before the second model call."""
        self._model.interrupt_before_second_model_call = True
        await self.engine.start(
            {"incident_id": incident_id, "service": "order-service"}
        )
        state = await self.engine.load(incident_id)
        return RecoveryRunResult(state, self._toolkit.count(tool_name))

    async def resume(self, incident_id: str) -> RecoveryRunResult:
        """Resume a previously interrupted investigation."""
        self._model.interrupt_before_second_model_call = False
        state = await self.engine.resume(incident_id)
        assert state is not None
        return RecoveryRunResult(state, self._toolkit.total_count)

    async def run_with_model_timeout(self, incident_id: str) -> RecoveryRunResult:
        """Start an investigation that will timeout on the second model call."""
        self._model.timeout_before_second_model_call = True
        await self.engine.start(
            {"incident_id": incident_id, "service": "order-service"}
        )
        state = await self.engine.load(incident_id)
        return RecoveryRunResult(state, self._toolkit.total_count)

    async def resume_with_healthy_model(self, incident_id: str) -> RecoveryRunResult:
        """Resume after a timeout, now with a healthy model."""
        self._model.timeout_before_second_model_call = False
        return await self.resume(incident_id)

    async def insert_corrupt_checkpoint(self, incident_id: str) -> None:
        """Overwrite the latest checkpoint blob with invalid data."""
        await self._checkpoints.saver.conn.execute(
            """
            UPDATE checkpoints
            SET checkpoint = ?
            WHERE thread_id = ?
              AND checkpoint_id = (
                SELECT checkpoint_id
                FROM checkpoints
                WHERE thread_id = ?
                ORDER BY checkpoint_id DESC
                LIMIT 1
              )
            """,
            (b"{invalid", incident_id, incident_id),
        )
        await self._checkpoints.saver.conn.commit()


# ---------------------------------------------------------------------------
# AgentHarness (original)
# ---------------------------------------------------------------------------


@dataclass
class AgentHarness:
    """Test harness owning real infrastructure and fake model injection.

    Provides methods to build the agent graph, create initial state,
    and run guard decisions for report gating tests.
    """

    checkpointer: Any
    tools: list[BaseTool]
    skills: SkillRuntime
    audit_store: InvestigationAuditStore
    toolkit: ReadOnlyToolkit
    evidence_recorder: EvidenceRecorder

    def fake_model(self, responses: list[AIMessage]) -> ScriptedChatModel:
        """Create a ScriptedChatModel with the given response sequence."""
        return ScriptedChatModel(responses=responses)

    def build(self, model: BaseChatModel) -> Any:
        """Build the investigation agent graph with the given model."""
        return build_investigation_agent(
            model=model,
            tools=self.tools,
            skill_runtime=self.skills,
            checkpointer=self.checkpointer,
            audit_store=self.audit_store,
            allow_fallback=False,
        )

    def initial_state(self, incident_id: str) -> IncidentAgentState:
        """Return a valid initial IncidentAgentState for testing."""
        return {
            "messages": [HumanMessage(content="Investigate the current incident.")],
            "incident_id": incident_id,
            "status": "investigating",
            "phase": "agent_loop",
            "alert": {"service": "order-service"},
            "current_round": 1,
            "max_rounds": 8,
            "hypotheses": [],
            "evidence": [],
            "retrieved_cases": [],
            "loaded_skill_names": [],
            "model_profile": "test",
            "model_call_count": 0,
            "tool_call_count": 0,
            "fallback_used": False,
            "report": None,
        }

    def guard(
        self,
        state: IncidentAgentState,
        *,
        cause_code: str,
        evidence_ids: list[str],
    ) -> Any:
        """Run the report gate check on a proposed root cause."""
        # Handle empty evidence_ids directly (before Pydantic validation)
        if not evidence_ids:
            from incidentlens_control_plane.agent.middleware import GuardDecision
            return GuardDecision(
                allowed=False,
                reason="current_incident_evidence_required",
            )

        proposal = RootCauseProposal(
            root_service="payment-service",
            cause_code=cause_code,
            evidence_ids=evidence_ids,
            confidence=0.8,
            next_action="finish",
        )
        return can_generate_guarded_report(
            state,
            proposal,
            self.skills.policies_by_cause_code,
        )

    def endless_tool_model(self) -> ScriptedChatModel:
        """Create a model that always returns tool calls (never finishes)."""
        return self.fake_model(
            [
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "search_logs",
                        "args": {
                            "incident_id": "inc-4",
                            "service": "order-service",
                            "keyword": f"timeout-{index}",
                            "limit": 10,
                        },
                        "id": f"call-{index}",
                        "type": "tool_call",
                    }],
                )
                for index in range(20)
            ]
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def agent_harness(tmp_path: Path, toolkit, investigation_audit_store) -> AgentHarness:
    """Async fixture that sets up the full agent infrastructure.

    Yields an AgentHarness with real SQLite checkpointer, real tools,
    real SkillRuntime, and methods for fake model injection.
    """
    db_path = tmp_path / "graph.db"

    async with AgentCheckpointRuntime(db_path) as cp:
        # Build evidence recorder and tools
        evidence_recorder = EvidenceRecorder(investigation_audit_store)

        # Build SkillRuntime with the skills directory
        skills_root = Path(__file__).resolve().parents[2] / "skills"
        skills = SkillRuntime(skills_root, investigation_audit_store)
        skills.validate()

        tools = build_agent_tools(toolkit, evidence_recorder, skills)

        harness = AgentHarness(
            checkpointer=cp.saver,
            tools=tools,
            skills=skills,
            audit_store=investigation_audit_store,
            toolkit=toolkit,
            evidence_recorder=evidence_recorder,
        )

        yield harness


@pytest.fixture
async def recovery_harness(
    tmp_path: Path,
    toolkit,
    investigation_audit_store,
) -> RecoveryHarness:
    """Async fixture that sets up the full recovery test infrastructure.

    Opens a real AgentCheckpointRuntime, builds the real Agent graph and
    runtime, and yields a RecoveryHarness for interrupt/resume testing.
    """
    from incidentlens_control_plane.agent.runtime import LLMInvestigationEngine

    db_path = tmp_path / "recovery_graph.db"
    counted = CountingToolkit(toolkit)

    async with AgentCheckpointRuntime(db_path) as cp:
        # Build evidence recorder and tools backed by the CountingToolkit
        evidence_recorder = EvidenceRecorder(investigation_audit_store)

        # Build SkillRuntime
        skills_root = Path(__file__).resolve().parents[2] / "skills"
        skills = SkillRuntime(skills_root, investigation_audit_store)
        skills.validate()

        tools = build_agent_tools(counted, evidence_recorder, skills)

        # Build the scripted model
        scripted = InterruptibleScriptedChatModel(responses=[])

        # Build the graph
        graph = build_investigation_agent(
            model=scripted,
            tools=tools,
            skill_runtime=skills,
            checkpointer=cp.saver,
            audit_store=investigation_audit_store,
            allow_fallback=False,
        )

        # Build the runtime with a short timeout for testing
        engine = LLMInvestigationEngine(
            graph=graph,
            audit_store=investigation_audit_store,
            model_identity=ModelIdentity(
                profile="test-model",
                model="test",
                endpoint_host="localhost",
            ),
            case_repository=None,
            total_timeout_seconds=5.0,
        )

        harness = RecoveryHarness(
            engine=engine,
            counted_toolkit=counted,
            checkpoint_runtime=cp,
            scripted_model=scripted,
        )

        yield harness
