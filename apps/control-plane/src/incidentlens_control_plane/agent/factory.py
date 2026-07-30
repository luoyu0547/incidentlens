"""Factory for building investigation engine runtimes.

Branches on ``RuntimeMode`` to return either the synchronous baseline
(wrapped in ``AsyncBaselineAdapter``) or the LLM-backed engine.
"""

from __future__ import annotations

from typing import Any

from incidentlens_control_plane.agent.baseline import DeterministicInvestigationEngine
from incidentlens_control_plane.agent.runtime import (
    AsyncBaselineAdapter,
    InvestigationEngineProtocol,
    LLMInvestigationEngine,
)
from incidentlens_control_plane.agent.state import (
    InvestigationAuditStore,
)
from incidentlens_control_plane.llm.config import RuntimeMode
from incidentlens_control_plane.llm.registry import ModelRegistry
from incidentlens_control_plane.memory.repository import CaseRepository
from incidentlens_control_plane.tools.query import ReadOnlyToolkit


def build_investigation_engine(
    *,
    mode: RuntimeMode,
    telemetry_repo: Any,
    toolkit: ReadOnlyToolkit,
    case_repository: CaseRepository | None,
    audit_store: InvestigationAuditStore,
    checkpointer: Any | None = None,
    skill_runtime: Any | None = None,
    model_registry: ModelRegistry | None = None,
) -> InvestigationEngineProtocol:
    """Build an investigation engine runtime for the given mode.

    Parameters
    ----------
    mode:
        Which runtime to construct.  ``DETERMINISTIC_BASELINE`` returns
        an ``AsyncBaselineAdapter`` wrapping the synchronous engine.
        ``LLM_AGENT`` returns a full ``LLMInvestigationEngine``.
    telemetry_repo:
        Telemetry repository (used by the baseline engine).
    toolkit:
        Read-only query toolkit.
    case_repository:
        Optional case memory repository.
    audit_store:
        Shared audit store.
    checkpointer:
        LangGraph checkpointer (required for LLM_AGENT mode).
    skill_runtime:
        SkillRuntime (required for LLM_AGENT mode).
    model_registry:
        ModelRegistry (required for LLM_AGENT mode).

    Returns
    -------
    InvestigationEngineProtocol
    """
    if mode == RuntimeMode.LLM_AGENT:
        if model_registry is None:
            raise ValueError(
                "ModelRegistry is required for LLM_AGENT mode. "
                "Pass model_registry to build_investigation_engine."
            )
        if checkpointer is None:
            raise ValueError(
                "A LangGraph checkpointer is required for LLM_AGENT mode."
            )
        if skill_runtime is None:
            raise ValueError(
                "A SkillRuntime is required for LLM_AGENT mode."
            )

        model = model_registry.get()
        identity = model_registry.identity()

        # Import here to avoid circular imports at module level
        from incidentlens_control_plane.agent.graph import build_investigation_agent
        from incidentlens_control_plane.agent.tool_adapter import (
            EvidenceRecorder,
            build_agent_tools,
        )

        evidence_recorder = EvidenceRecorder(audit_store)
        tools = build_agent_tools(toolkit, evidence_recorder, skill_runtime)

        graph = build_investigation_agent(
            model=model,
            tools=tools,
            skill_runtime=skill_runtime,
            checkpointer=checkpointer,
            audit_store=audit_store,
            allow_fallback=False,
        )

        return LLMInvestigationEngine(  # type: ignore[return-value]
            graph=graph,
            audit_store=audit_store,
            model_identity=identity,
            case_repository=case_repository,
        )

    # DETERMINISTIC_BASELINE
    engine = DeterministicInvestigationEngine(
        telemetry_repo=telemetry_repo,
        toolkit=toolkit,
        case_repository=case_repository,
        audit_store=audit_store,
    )
    return AsyncBaselineAdapter(engine)
