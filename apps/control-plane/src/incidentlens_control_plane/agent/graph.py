"""Bounded LangChain agent graph for incident investigation.

Composes two phases:
  1. Investigation: gathers evidence using read-only observability tools
  2. Conclusion: emits a RootCauseProposal using only the proposal tool

The graph uses a deterministic readiness check to transition between phases,
ensuring the model cannot continue calling observability tools after
sufficient evidence has been gathered.

Uses only public LangChain/LangGraph APIs (create_agent, middleware).
"""

from __future__ import annotations

from typing import Any, Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from incidentlens_control_plane.agent.conclusion import (
    build_conclusion_context,
    classify_repair,
    evaluate_conclusion_readiness,
    parse_conclusion_output,
)
from incidentlens_control_plane.agent.middleware import (
    AuditMiddleware,
    BudgetEnforcementMiddleware,
    EvidenceRecordingMiddleware,
    ReportGateMiddleware,
)
from incidentlens_control_plane.agent.prompts import CONCLUSION_PROMPT, SYSTEM_PROMPT
from incidentlens_control_plane.agent.skills import SkillRuntime
from incidentlens_control_plane.agent.state import InvestigationAuditStore
from incidentlens_control_plane.agent.types import (
    IncidentAgentState,
    InvestigationContext,
    RootCauseProposal,
)


def build_conclusion_tool() -> BaseTool:
    """Build the RootCauseProposal structured tool for the conclusion phase."""
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        func=lambda **kwargs: kwargs,
        name="root_cause_proposal",
        description="Emit a structured root cause proposal for the incident.",
        args_schema=RootCauseProposal,
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

    The investigation agent uses read-only observability tools and does NOT
    force structured output.  It gathers evidence until the readiness check
    determines that a conclusion can be attempted.

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

    # Build the agent — NO ToolStrategy, NO response_format
    # The investigation agent gathers evidence only; it does NOT emit proposals
    agent = create_agent(
        model=model,
        tools=list(tools),
        system_prompt=SYSTEM_PROMPT,
        middleware=middleware,
        response_format=None,
        state_schema=IncidentAgentState,
        context_schema=InvestigationContext,
        checkpointer=checkpointer,
        name="incidentlens-investigator",
    )

    return agent


def build_conclusion_agent(
    *,
    model: BaseChatModel,
    skill_runtime: SkillRuntime,
    audit_store: InvestigationAuditStore,
) -> Any:
    """Build a conclusion-only agent that emits a RootCauseProposal.

    This agent binds exactly one tool: the schema-derived RootCauseProposal
    tool, with required tool use.  No observability tool is available.

    Parameters
    ----------
    model:
        The same configured model used for investigation.
    skill_runtime:
        SkillRuntime for evidence policy validation.
    audit_store:
        InvestigationAuditStore for recording audit entries.

    Returns
    -------
    A compiled LangGraph agent graph for the conclusion phase.
    """
    proposal_tool = build_conclusion_tool()

    middleware: list[AgentMiddleware] = []
    middleware.append(AuditMiddleware(audit_store))

    # Budget enforcement — conclusion calls count toward total budget
    middleware.append(BudgetEnforcementMiddleware(model_limit=12, tool_limit=12))

    # Report gate middleware — validates the proposal against evidence policies
    middleware.append(ReportGateMiddleware(skill_runtime))

    agent = create_agent(
        model=model,
        tools=[proposal_tool],
        system_prompt=CONCLUSION_PROMPT,
        middleware=middleware,
        response_format=ToolStrategy(RootCauseProposal),
        state_schema=IncidentAgentState,
        context_schema=InvestigationContext,
        checkpointer=None,  # No separate checkpointer — uses parent graph state
        name="incidentlens-concluder",
    )

    return agent
