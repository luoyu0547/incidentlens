"""Shared fixtures for agent graph tests.

Provides AgentHarness which owns the real SQLite checkpoint runtime,
real tool adapters, real SkillRuntime, and an injected ScriptedChatModel.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool

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
from incidentlens_control_plane.tools.query import ReadOnlyToolkit
import sys
from pathlib import Path

# Add project root to sys.path so that tests.support is importable
_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from tests.support.fake_chat_model import ScriptedChatModel


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
        tools = build_agent_tools(toolkit, evidence_recorder)

        # Build SkillRuntime with the skills directory
        skills_root = Path(__file__).resolve().parents[2] / "skills"
        skills = SkillRuntime(skills_root, investigation_audit_store)
        skills.validate()

        harness = AgentHarness(
            checkpointer=cp.saver,
            tools=tools,
            skills=skills,
            audit_store=investigation_audit_store,
            toolkit=toolkit,
            evidence_recorder=evidence_recorder,
        )

        yield harness
