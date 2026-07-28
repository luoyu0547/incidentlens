"""Bounded LangChain agent graph for incident investigation.

Composes the agent with:
  - System prompt and context builder
  - SkillRuntime middleware (filesystem, skills, audit)
  - AuditMiddleware for audit logging
  - EvidenceRecordingMiddleware for evidence extraction
  - BudgetEnforcementMiddleware for model/tool call limits
  - ReportGateMiddleware for evidence policy gating

Uses only public LangChain/LangGraph APIs (create_agent, middleware).
"""

from __future__ import annotations

from typing import Any, Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from incidentlens_control_plane.agent.middleware import (
    AuditMiddleware,
    BudgetEnforcementMiddleware,
    EvidenceRecordingMiddleware,
    ReportGateMiddleware,
)
from incidentlens_control_plane.agent.prompts import SYSTEM_PROMPT
from incidentlens_control_plane.agent.skills import SkillRuntime
from incidentlens_control_plane.agent.state import InvestigationAuditStore
from incidentlens_control_plane.agent.types import (
    IncidentAgentState,
    InvestigationContext,
    RootCauseProposal,
)


def build_investigation_agent(
    *,
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    skill_runtime: SkillRuntime,
    checkpointer: Any,
    audit_store: InvestigationAuditStore,
    fallback_models: Sequence[BaseChatModel] = (),
    allow_fallback: bool = True,
) -> Any:
    """Build a bounded investigation agent graph.

    Parameters
    ----------
    model:
        The primary LLM to use for investigation reasoning.
    tools:
        LangChain StructuredTool instances from build_agent_tools.
    skill_runtime:
        SkillRuntime providing filesystem, skills, and audit middleware.
    checkpointer:
        An AsyncSqliteSaver (or compatible) for state persistence.
    audit_store:
        InvestigationAuditStore for recording audit entries.
    fallback_models:
        Optional fallback models for transport errors.
    allow_fallback:
        Whether to add fallback model middleware. When False, no fallback
        middleware is added (used in testing).

    Returns
    -------
    A compiled LangGraph agent graph.
    """
    # Build middleware list
    middleware: list[AgentMiddleware] = []

    # Skill middleware (filesystem, skills, skill_read_audit)
    fs_middleware, skills_middleware, skill_audit = skill_runtime.middleware()
    middleware.extend([fs_middleware, skills_middleware, skill_audit])

    # Audit middleware
    middleware.append(AuditMiddleware(audit_store))

    # Evidence recording middleware
    middleware.append(EvidenceRecordingMiddleware())

    # Budget enforcement middleware
    middleware.append(BudgetEnforcementMiddleware(model_limit=12, tool_limit=12))

    # Report gate middleware
    middleware.append(ReportGateMiddleware(skill_runtime))

    # Build the agent
    agent = create_agent(
        model=model,
        tools=list(tools),
        system_prompt=SYSTEM_PROMPT,
        middleware=middleware,
        response_format=ToolStrategy(RootCauseProposal),
        state_schema=IncidentAgentState,
        context_schema=InvestigationContext,
        checkpointer=checkpointer,
        name="incidentlens-investigator",
    )

    return agent
