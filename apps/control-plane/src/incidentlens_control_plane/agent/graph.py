"""Bounded LangChain agent graph for incident investigation.

Composes the agent with middleware in deterministic order:
  1. Project filesystem/Skill middleware
  2. Project Memory injection
  3. Investigation context
  4. Audit/evidence/conclusion gates
  5. Compaction
  6. Report gate

Uses only public LangChain/LangGraph APIs (create_agent, middleware).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from incidentlens_control_plane.agent.middleware import (
    AuditMiddleware,
    BudgetEnforcementMiddleware,
    ConclusionBoundaryMiddleware,
    DuplicateToolCallMiddleware,
    EvidenceRecordingMiddleware,
    IncidentToolContextMiddleware,
    InvestigationContextMiddleware,
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
    project_memory_runtime: Any | None = None,
    compaction_runtime: Any | None = None,
    model_identity: Any | None = None,
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
    project_memory_runtime:
        Optional ProjectMemoryRuntime for memory injection middleware.
    compaction_runtime:
        Optional compaction runtime for context management middleware.
    model_identity:
        Optional ModelIdentity with context_window_tokens and
        reserved_output_tokens for compaction thresholds.

    Returns
    -------
    A compiled LangGraph agent graph.
    """
    # Build middleware list in deterministic order
    middleware: list[Any] = []

    # --- 1. Project filesystem/Skill middleware ---
    fs_middleware, skills_middleware, skill_audit = skill_runtime.middleware()
    middleware.extend([fs_middleware, skills_middleware, skill_audit])

    # --- 2. Project Memory injection ---
    if project_memory_runtime is not None:
        from incidentlens_control_plane.project_memory.middleware import (
            ProjectMemoryMiddleware,
        )

        memory_base = getattr(project_memory_runtime, "base_dir", Path("."))
        middleware.append(ProjectMemoryMiddleware(base_dir=memory_base))  # type: ignore[arg-type]

    # --- 3. Investigation context ---
    middleware.append(InvestigationContextMiddleware(skill_runtime=skill_runtime))  # type: ignore[arg-type]

    # --- 4. Audit/evidence/conclusion gates ---
    middleware.append(AuditMiddleware(audit_store))  # type: ignore[arg-type]
    middleware.append(IncidentToolContextMiddleware())  # type: ignore[arg-type]
    middleware.append(DuplicateToolCallMiddleware())  # type: ignore[arg-type]
    middleware.append(EvidenceRecordingMiddleware())  # type: ignore[arg-type]
    middleware.append(ConclusionBoundaryMiddleware())  # type: ignore[arg-type]
    middleware.append(BudgetEnforcementMiddleware(model_limit=12, tool_limit=12))  # type: ignore[arg-type]

    # --- 5. Compaction ---
    if compaction_runtime is not None:
        from incidentlens_control_plane.compaction.middleware import (
            CompactionMiddleware,
            TranscriptStore,
        )
        from incidentlens_control_plane.compaction.session import (
            SessionMemoryStore,
        )
        from incidentlens_control_plane.compaction.tool_budget import (
            ToolOutputStore,
        )

        compaction_dir = Path(
            getattr(compaction_runtime, "session_dir", ".incidentlens/sessions")
        )
        transcript_dir = Path(
            getattr(compaction_runtime, "transcript_dir", ".incidentlens/transcripts")
        )
        task_output_dir = Path(
            getattr(compaction_runtime, "task_output_dir", ".incidentlens/task-outputs")
        )

        session_store = SessionMemoryStore(base_dir=compaction_dir)
        transcript_store = TranscriptStore(base_dir=transcript_dir)
        tool_output_store = ToolOutputStore(base_dir=task_output_dir)

        # Build model profile for compaction threshold
        model_profile = None
        if model_identity is not None:
            ctx_tokens = getattr(model_identity, "context_window_tokens", 128_000)
            res_tokens = getattr(model_identity, "reserved_output_tokens", 4_096)

            class _ModelProfile:
                context_window_tokens = ctx_tokens
                reserved_output_tokens = res_tokens

            model_profile = _ModelProfile()

        middleware.append(CompactionMiddleware(  # type: ignore[arg-type]
            runtime=None,
            model_profile=model_profile,
            session_store=session_store,
            tool_output_store=tool_output_store,
            transcript_store=transcript_store,
        ))

    # --- 6. Report gate ---
    middleware.append(ReportGateMiddleware(skill_runtime, audit_store=audit_store))  # type: ignore[arg-type]

    # Build the agent
    agent = create_agent(  # type: ignore[misc]
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
