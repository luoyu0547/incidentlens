"""LLM investigation engine runtime and shared async protocol.

Provides:
  - ``InvestigationEngineProtocol`` -- async protocol for all engine runtimes
  - ``LLMInvestigationEngine`` -- LangGraph-based async investigation engine
  - ``AsyncBaselineAdapter`` -- wraps the synchronous baseline for the shared API
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from incidentlens_contracts.models import InvestigationStatus

from incidentlens_control_plane.agent.baseline import DeterministicInvestigationEngine
from incidentlens_control_plane.agent.state import (
    InvestigationAuditStore,
    InvestigationState,
)
from incidentlens_control_plane.llm.config import RuntimeMode
from incidentlens_control_plane.llm.registry import ModelIdentity
from incidentlens_control_plane.memory.repository import CaseRepository


# ---------------------------------------------------------------------------
# Shared async protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class InvestigationEngineProtocol(Protocol):
    """Async protocol that every investigation engine runtime must satisfy."""

    mode: RuntimeMode
    audit_store: InvestigationAuditStore

    async def start(self, alert: dict[str, Any]) -> InvestigationState: ...
    async def run_round(self, incident_id: str) -> InvestigationState: ...
    async def resume(self, incident_id: str) -> InvestigationState | None: ...
    async def load(self, incident_id: str) -> InvestigationState | None: ...


# ---------------------------------------------------------------------------
# LLM Investigation Engine
# ---------------------------------------------------------------------------


class LLMInvestigationEngine:
    """LangGraph-based async investigation engine.

    Delegates reasoning to a compiled LangGraph agent graph and
    relies on LangGraph's checkpoint saver for state persistence.
    The runtime never calls ``CheckpointStore.save`` directly.
    """

    mode = RuntimeMode.LLM_AGENT

    def __init__(
        self,
        *,
        graph: Any,
        audit_store: InvestigationAuditStore,
        model_identity: ModelIdentity,
        case_repository: CaseRepository | None,
        total_timeout_seconds: float = 1200,
    ) -> None:
        self._graph = graph
        self._audit_store = audit_store
        self._model_identity = model_identity
        self._case_repository = case_repository
        self._total_timeout_seconds = total_timeout_seconds

    @property
    def audit_store(self) -> InvestigationAuditStore:
        return self._audit_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, alert: dict[str, Any]) -> InvestigationState:
        """Start a new LLM-backed investigation.

        Generates the incident ID, validates the alert, queries
        ``case_repository`` when present, filters to ``human_verified``
        cases, builds the initial state, and invokes the graph.
        """
        incident_id = str(uuid4())

        # Build initial state
        state_dict: dict[str, Any] = {
            "incident_id": incident_id,
            "status": InvestigationStatus.SCOPING.value,
            "phase": "parse_alert",
            "alert": alert,
            "current_round": 0,
            "max_rounds": 8,
            "hypotheses": [],
            "evidence": [],
            "retrieved_cases": [],
            "loaded_skill_names": [],
            "model_profile": self._model_identity.profile,
            "model_call_count": 0,
            "tool_call_count": 0,
            "fallback_used": False,
            "report": None,
        }

        # Query historical cases when available
        if self._case_repository is not None:
            service = alert.get("service", "")
            symptom = alert.get("symptom", "")
            query_parts = []
            if symptom:
                query_parts.append(symptom)
            for key in ("error_rate", "latency", "error"):
                if key in alert:
                    query_parts.append(str(alert[key]))
            query = " ".join(query_parts) if query_parts else service
            if query:
                cases = self._case_repository.search(query, service, None)
                # Filter to human_verified cases only
                state_dict["retrieved_cases"] = [
                    {
                        "case_id": c.id,
                        "symptom": c.symptom,
                        "root_cause": c.root_cause,
                        "service": c.service,
                    }
                    for c in cases
                    if c.status == "human_verified"
                ]

        self._audit_store.record(
            incident_id,
            "phase_transition",
            {"from": "init", "to": "agent_loop"},
        )

        # Invoke the graph
        await self._graph.ainvoke(
            state_dict,
            config={"configurable": {"thread_id": incident_id}},
        )

        # Project the saved state from the graph
        saved = await self._project_state(incident_id)
        return saved

    async def run_round(self, incident_id: str) -> InvestigationState:
        """Resume the graph for one bounded agent invocation."""
        current = await self._project_state(incident_id)
        if current is None:
            raise ValueError(f"No investigation found for incident_id={incident_id}")

        # Check terminal
        if current.status in (
            InvestigationStatus.REPORT_READY,
            InvestigationStatus.NEEDS_MORE_EVIDENCE,
        ):
            return current

        self._audit_store.record(
            incident_id,
            "phase_transition",
            {"from": current.phase, "to": "agent_loop"},
        )

        await self._graph.ainvoke(
            None,
            config={"configurable": {"thread_id": incident_id}},
        )

        return await self._project_state(incident_id)

    async def resume(self, incident_id: str) -> InvestigationState | None:
        """Load the latest graph state and continue only if non-terminal."""
        saved = await self._project_state(incident_id)
        if saved is None:
            return None

        # If terminal, return as-is without restarting
        if saved.status in (
            InvestigationStatus.REPORT_READY,
            InvestigationStatus.NEEDS_MORE_EVIDENCE,
        ):
            return saved

        return await self.run_round(incident_id)

    async def load(self, incident_id: str) -> InvestigationState | None:
        """Load the current LangGraph state without advancing."""
        return await self._project_state(incident_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _project_state(self, incident_id: str) -> InvestigationState | None:
        """Read the LangGraph checkpoint and project to InvestigationState."""
        config = {"configurable": {"thread_id": incident_id}}
        snapshot = await self._graph.aget_state(config)
        values = snapshot.values if snapshot else None
        if values is None:
            return None

        # Handle both dict and SimpleNamespace-style values
        if isinstance(values, dict):
            status_raw = values.get("status", InvestigationStatus.SCOPING.value)
            report = values.get("report")
        else:
            status_raw = getattr(values, "status", InvestigationStatus.SCOPING.value)
            report = getattr(values, "report", None)

        # Normalise status to enum
        if isinstance(status_raw, str):
            try:
                status = InvestigationStatus(status_raw)
            except ValueError:
                status = InvestigationStatus.SCOPING
        else:
            status = status_raw

        def _get(key: str, default: Any = None) -> Any:
            if isinstance(values, dict):
                return values.get(key, default)
            return getattr(values, key, default)

        return InvestigationState(
            incident_id=_get("incident_id", incident_id),
            status=status,
            current_round=_get("current_round", 0),
            max_rounds=_get("max_rounds", 8),
            alert=_get("alert", {}),
            hypotheses=_get("hypotheses", []),
            evidence=_get("evidence", []),
            report=report,
            phase=_get("phase", "agent_loop"),
            retrieved_cases=_get("retrieved_cases", []),
            loaded_skill_names=_get("loaded_skill_names", []),
            model_profile=_get("model_profile", ""),
            model_call_count=_get("model_call_count", 0),
            tool_call_count=_get("tool_call_count", 0),
            fallback_used=_get("fallback_used", False),
        )


# ---------------------------------------------------------------------------
# Async Baseline Adapter
# ---------------------------------------------------------------------------


class AsyncBaselineAdapter:
    """Wraps the synchronous DeterministicInvestigationEngine for the shared async API."""

    mode = RuntimeMode.DETERMINISTIC_BASELINE

    def __init__(self, delegate: DeterministicInvestigationEngine) -> None:
        self._delegate = delegate
        self.audit_store = delegate.audit_store

    async def start(self, alert: dict[str, Any]) -> InvestigationState:
        return self._delegate.start(alert)

    async def run_round(self, incident_id: str) -> InvestigationState:
        return await self._delegate.run_round(incident_id)

    async def resume(self, incident_id: str) -> InvestigationState | None:
        return await self._delegate.resume(incident_id)

    async def load(self, incident_id: str) -> InvestigationState | None:
        return self._delegate.checkpoint_store.load(incident_id)
