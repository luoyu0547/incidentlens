"""LLM investigation engine runtime and shared async protocol.

Provides:
  - ``InvestigationEngineProtocol`` -- async protocol for all engine runtimes
  - ``LLMInvestigationEngine`` -- LangGraph-based async investigation engine
  - ``AsyncBaselineAdapter`` -- wraps the synchronous baseline for the shared API
  - ``InvestigationError`` -- base exception for investigation failures
  - ``CheckpointCorruptError`` -- re-export from checkpoint module
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from incidentlens_contracts.models import InvestigationStatus
from langgraph.errors import EmptyInputError
from langgraph.types import Command

from incidentlens_control_plane.agent.baseline import DeterministicInvestigationEngine
from incidentlens_control_plane.agent.checkpoint import CheckpointCorruptError
from incidentlens_control_plane.agent.state import (
    InvestigationAuditStore,
    InvestigationState,
)
from incidentlens_control_plane.llm.config import RuntimeMode
from incidentlens_control_plane.llm.registry import ModelIdentity
from incidentlens_control_plane.memory.repository import CaseRepository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------

ERROR_MODEL_TIMEOUT = "model_timeout"
ERROR_MODEL_UNAVAILABLE = "model_unavailable"
ERROR_BUDGET_EXHAUSTED = "budget_exhausted"
ERROR_MODEL_OUTPUT_INVALID = "model_output_invalid"
ERROR_SKILL_LOAD_FAILED = "skill_load_failed"
ERROR_CHECKPOINT_CORRUPT = "checkpoint_corrupt"

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

    The engine implements a two-phase flow:
      1. Investigation: gathers evidence using observability tools
      2. Conclusion: emits a RootCauseProposal using only the proposal tool
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
        conclusion_agent: Any | None = None,
        skill_runtime: Any | None = None,
    ) -> None:
        self._graph = graph
        self._audit_store = audit_store
        self._model_identity = model_identity
        self._case_repository = case_repository
        self._total_timeout_seconds = total_timeout_seconds
        self._conclusion_agent = conclusion_agent
        self._skill_runtime = skill_runtime

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
        # Use incident_id from alert if provided, otherwise generate one
        incident_id = alert.get("incident_id") or str(uuid4())

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

        # Invoke the graph with total timeout
        config = {"configurable": {"thread_id": incident_id}}
        try:
            async with asyncio.timeout(self._total_timeout_seconds):
                await self._graph.ainvoke(state_dict, config)
        except TimeoutError:
            await self._record_recoverable_failure(
                incident_id,
                code=ERROR_MODEL_TIMEOUT,
                safe_details={"profile": self._model_identity.profile},
            )
            return await self._load_projected_state(incident_id)

        # Project the saved state from the graph
        saved = await self._project_state(incident_id)
        return saved

    async def run_round(self, incident_id: str) -> InvestigationState:
        """Resume the graph for one bounded agent invocation.

        After the investigation phase, checks conclusion readiness and
        runs the conclusion phase if eligible.
        """
        current = await self._project_state(incident_id)
        if current is None:
            raise ValueError(f"No investigation found for incident_id={incident_id}")

        # Check terminal
        if current.status in (
            InvestigationStatus.REPORT_READY,
            InvestigationStatus.NEEDS_MORE_EVIDENCE,
        ):
            return current

        # Check if conclusion is already accepted — don't restart
        if current.conclusion_status == "accepted":
            return current

        # Check if conclusion failed terminally — don't restart
        if current.conclusion_status == "rejected" and current.conclusion_attempt_count >= 2:
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

        # Check conclusion readiness and run conclusion if eligible
        if self._conclusion_agent is not None and self._skill_runtime is not None:
            await self._maybe_run_conclusion(incident_id)

        return await self._project_state(incident_id)

    async def resume(self, incident_id: str) -> InvestigationState | None:
        """Load the latest graph state and continue only if non-terminal.

        If the snapshot contains a pending interrupt, resumes with
        ``Command(resume=True)``.  Otherwise invokes the normal bounded
        continuation.
        """
        saved = await self._project_state(incident_id)
        if saved is None:
            return None

        # If terminal, return as-is without restarting
        if saved.status in (
            InvestigationStatus.REPORT_READY,
            InvestigationStatus.NEEDS_MORE_EVIDENCE,
        ):
            return saved

        # Check if conclusion is already accepted — don't restart
        if saved.conclusion_status == "accepted":
            return saved

        # Check if conclusion failed terminally — don't restart
        if saved.conclusion_status == "rejected" and saved.conclusion_attempt_count >= 2:
            return saved

        config = {"configurable": {"thread_id": incident_id}}
        snapshot = await self._graph.aget_state(config)

        # Detect pending interrupt
        try:
            if snapshot and snapshot.interrupts:
                await self._graph.ainvoke(Command(resume=True), config)
            else:
                await self._graph.ainvoke(None, config)
        except EmptyInputError as exc:
            raise CheckpointCorruptError(incident_id, str(exc)) from exc

        # Check conclusion readiness and run conclusion if eligible
        if self._conclusion_agent is not None and self._skill_runtime is not None:
            await self._maybe_run_conclusion(incident_id)

        return await self._project_state(incident_id)

    async def load(self, incident_id: str) -> InvestigationState | None:
        """Load the current LangGraph state without advancing."""
        return await self._project_state(incident_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _maybe_run_conclusion(self, incident_id: str) -> None:
        """Check conclusion readiness and run conclusion phase if eligible.

        This method:
        1. Evaluates conclusion readiness using the deterministic evaluator
        2. If ready, runs the conclusion agent with only the proposal tool
        3. Parses the result and applies the report gate
        4. Records audit entries for every transition
        """
        from incidentlens_control_plane.agent.conclusion import (
            build_conclusion_context,
            classify_repair,
            evaluate_conclusion_readiness,
            parse_conclusion_output,
        )

        config = {"configurable": {"thread_id": incident_id}}
        snapshot = await self._graph.aget_state(config)
        values = snapshot.values if snapshot else None
        if values is None:
            return

        def _get(key: str, default: Any = None) -> Any:
            if isinstance(values, dict):
                return values.get(key, default)
            return getattr(values, key, default)

        # Skip if already in conclusion phase
        conclusion_status = _get("conclusion_status", "not_ready")
        if conclusion_status in ("accepted", "attempting"):
            return

        # Skip if conclusion failed terminally
        conclusion_attempt_count = _get("conclusion_attempt_count", 0)
        if conclusion_status == "rejected" and conclusion_attempt_count >= 2:
            return

        # Evaluate readiness
        loaded_skill_names = _get("loaded_skill_names", [])
        evidence = _get("evidence", [])
        alert = _get("alert", {})

        readiness = evaluate_conclusion_readiness(
            loaded_skill_names=loaded_skill_names,
            policies_by_cause_code=self._skill_runtime.policies_by_cause_code,
            evidence=evidence,
            incident_id=incident_id,
        )

        if not readiness.ready:
            return

        # Record conclusion boundary
        self._audit_store.record(
            incident_id,
            "conclusion_boundary_entered",
            {
                "eligible_cause_codes": readiness.eligible_cause_codes,
                "eligible_evidence_count": len(readiness.eligible_evidence_ids),
            },
        )

        # Update state to attempting
        await self._graph.aupdate_state(config, {
            "conclusion_status": "attempting",
            "conclusion_attempt_count": conclusion_attempt_count + 1,
            "eligible_cause_codes": readiness.eligible_cause_codes,
            "eligible_evidence_ids": readiness.eligible_evidence_ids,
        })

        # Build conclusion context
        conclusion_context = build_conclusion_context(
            incident_id=incident_id,
            alert=alert,
            loaded_skill_names=loaded_skill_names,
            eligible_cause_codes=readiness.eligible_cause_codes,
            material_evidence=[
                ev for ev in evidence
                if ev.content.get("incident_id") == incident_id
                and ev.content.get("outcome") != "invalid_arguments"
                and ev.content.get("outcome") != "error"
                and (ev.content.get("data") or ev.content.get("count"))
            ],
            eligible_evidence_ids=readiness.eligible_evidence_ids,
        )

        # Run the conclusion agent
        from langchain_core.messages import HumanMessage

        try:
            result = await self._conclusion_agent.ainvoke(
                {
                    "messages": [HumanMessage(content=conclusion_context)],
                    "incident_id": incident_id,
                    "status": "concluding",
                    "phase": "conclusion",
                    "alert": alert,
                    "current_round": _get("current_round", 0),
                    "max_rounds": _get("max_rounds", 8),
                    "hypotheses": _get("hypotheses", []),
                    "evidence": evidence,
                    "retrieved_cases": _get("retrieved_cases", []),
                    "loaded_skill_names": loaded_skill_names,
                    "model_profile": _get("model_profile", ""),
                    "model_call_count": 0,
                    "tool_call_count": 0,
                    "fallback_used": False,
                    "report": None,
                },
                config,
            )
        except TimeoutError:
            await self._record_recoverable_failure(
                incident_id,
                code=ERROR_MODEL_TIMEOUT,
                safe_details={"profile": self._model_identity.profile, "phase": "conclusion"},
            )
            return

        # Extract the structured response
        proposal = None
        if isinstance(result, dict):
            proposal = result.get("structured_response") or result.get("report")
        elif hasattr(result, "structured_response"):
            proposal = result.structured_response

        if proposal is None:
            # Try to extract from messages
            messages = result.get("messages", []) if isinstance(result, dict) else []
            for msg in reversed(messages):
                if hasattr(msg, "tool_calls"):
                    for tc in msg.tool_calls:
                        if tc.get("name") == "root_cause_proposal":
                            try:
                                from incidentlens_control_plane.agent.types import RootCauseProposal
                                proposal = RootCauseProposal.model_validate(tc.get("args", {}))
                            except Exception:
                                pass
                            break
                if proposal is not None:
                    break

        if proposal is None:
            # No proposal — record invalid output
            self._audit_store.record(
                incident_id,
                "structured_output_invalid",
                {"attempt": conclusion_attempt_count + 1, "error": "no_proposal_tool_call"},
            )
            await self._graph.aupdate_state(config, {
                "conclusion_status": "rejected",
                "last_error_code": ERROR_MODEL_OUTPUT_INVALID,
            })
            return

        # Parse and validate the proposal
        parse_result = parse_conclusion_output(
            proposal,
            eligible_cause_codes=readiness.eligible_cause_codes,
            eligible_evidence_ids=readiness.eligible_evidence_ids,
        )

        if not parse_result.success:
            # Check if repairable
            repair = classify_repair(
                parse_result,
                eligible_cause_codes=readiness.eligible_cause_codes,
                eligible_evidence_ids=readiness.eligible_evidence_ids,
            )

            self._audit_store.record(
                incident_id,
                "structured_output_invalid",
                {
                    "attempt": conclusion_attempt_count + 1,
                    "error_code": parse_result.error_code,
                    "repairable": repair.repairable,
                },
            )

            if repair.repairable and conclusion_attempt_count < 2:
                # Allow one repair — update state and return
                await self._graph.aupdate_state(config, {
                    "conclusion_status": "attempting",
                    "last_error_code": parse_result.error_code,
                })
                return
            else:
                # Terminal failure
                await self._graph.aupdate_state(config, {
                    "conclusion_status": "rejected",
                    "last_error_code": ERROR_MODEL_OUTPUT_INVALID,
                })
                return

        # Valid proposal — apply report gate
        from incidentlens_control_plane.agent.middleware import can_generate_guarded_report

        raw_proposal = parse_result.proposal
        gate_decision = can_generate_guarded_report(
            {"evidence": evidence, "incident_id": incident_id},
            raw_proposal,
            self._skill_runtime.policies_by_cause_code,
        )

        if not gate_decision.allowed:
            self._audit_store.record(
                incident_id,
                "report_gate_rejected",
                {
                    "attempt": conclusion_attempt_count + 1,
                    "reason": gate_decision.reason,
                    "cause_code": raw_proposal.cause_code,
                },
            )

            if conclusion_attempt_count < 2:
                # Allow one repair
                await self._graph.aupdate_state(config, {
                    "conclusion_status": "attempting",
                    "last_report_rejection_reason": gate_decision.reason,
                })
                return
            else:
                # Terminal rejection
                await self._graph.aupdate_state(config, {
                    "conclusion_status": "rejected",
                    "last_error_code": ERROR_BUDGET_EXHAUSTED,
                    "last_report_rejection_reason": gate_decision.reason,
                })
                return

        # Proposal accepted
        self._audit_store.record(
            incident_id,
            "report_gate_accepted",
            {
                "cause_code": raw_proposal.cause_code,
                "evidence_ids": raw_proposal.evidence_ids,
                "confidence": raw_proposal.confidence,
            },
        )

        # Build the report
        report = {
            "root_service": raw_proposal.root_service,
            "root_cause": raw_proposal.cause_code,
            "evidence_ids": raw_proposal.evidence_ids,
            "confidence": raw_proposal.confidence,
            "next_action": raw_proposal.next_action,
        }

        # Determine final status
        if raw_proposal.next_action == "finish":
            final_status = InvestigationStatus.REPORT_READY
        else:
            final_status = InvestigationStatus.NEEDS_MORE_EVIDENCE

        await self._graph.aupdate_state(config, {
            "conclusion_status": "accepted",
            "status": final_status.value,
            "report": report,
            "phase": "conclusion_complete",
        })

    async def _load_projected_state(self, incident_id: str) -> InvestigationState | None:
        """Alias for _project_state; loads state without advancing the graph."""
        return await self._project_state(incident_id)

    async def _record_recoverable_failure(
        self,
        incident_id: str,
        code: str,
        safe_details: dict[str, Any] | None = None,
    ) -> None:
        """Record a recoverable failure and persist the error code.

        Updates the graph state with ``last_error_code`` and records an
        audit entry.  No secrets or complete request content is stored.
        """
        config = {"configurable": {"thread_id": incident_id}}
        await self._graph.aupdate_state(config, {"last_error_code": code})
        self._audit_store.record(
            incident_id,
            "recoverable_failure",
            {"code": code, **(safe_details or {})},
        )

    async def _project_state(self, incident_id: str) -> InvestigationState | None:
        """Read the LangGraph checkpoint and project to InvestigationState."""
        config = {"configurable": {"thread_id": incident_id}}
        try:
            snapshot = await self._graph.aget_state(config)
        except Exception as exc:
            raise CheckpointCorruptError(incident_id, str(exc)) from exc

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

        # Extract checkpoint_id from snapshot config
        checkpoint_id = None
        if snapshot and hasattr(snapshot, "config") and snapshot.config:
            checkpoint_id = snapshot.config.get("configurable", {}).get("checkpoint_id")

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
            last_error_code=_get("last_error_code"),
            last_checkpoint_id=checkpoint_id,
            conclusion_status=_get("conclusion_status", "not_ready"),
            conclusion_attempt_count=_get("conclusion_attempt_count", 0),
            eligible_cause_codes=_get("eligible_cause_codes", []),
            eligible_evidence_ids=_get("eligible_evidence_ids", []),
            last_report_rejection_reason=_get("last_report_rejection_reason"),
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
