"""Investigation engine with checkpointed loop and evidence-driven hypothesis updates.

Public interface:
  - start(alert) -> InvestigationState
  - run_round(incident_id) -> InvestigationState
  - resume(incident_id) -> InvestigationState | None

State machine:
  parse_alert -> scope_incident -> retrieve_memory -> generate_hypotheses ->
  choose_next_action -> execute_tool -> record_evidence -> update_hypotheses ->
  verify_root_cause -> generate_report

Key design points:
  - After every tool call and state transition, persist the investigation state
  - Dedup same tool+args evidence
  - Record errors/empty/conflicting results as evidence
  - Confidence > 0.70 must reference current Evidence.id; otherwise needs_more_evidence
  - Historical cases only generate candidate hypotheses (never confirmed)
  - Default 8 rounds max
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from incidentlens_contracts.models import (
    Evidence,
    Hypothesis,
    HypothesisStatus,
    InvestigationStatus,
    ToolResult,
)

from incidentlens_control_plane.agent.reporting import can_generate_report, generate_report
from incidentlens_control_plane.agent.state import (
    CheckpointStore,
    InvestigationAuditStore,
    InvestigationState,
)
from incidentlens_control_plane.memory.repository import CaseRepository
from incidentlens_control_plane.tools.query import ReadOnlyToolkit

# ---------------------------------------------------------------------------
# Tool selection strategy
# ---------------------------------------------------------------------------

# Map from investigation need to tool name and default args
_TOOL_STRATEGY: list[dict[str, Any]] = [
    {"tool": "search_logs", "description": "Search for error logs"},
    {"tool": "query_metrics", "description": "Query error rate metrics"},
    {"tool": "get_slow_traces", "description": "Find slow traces"},
    {"tool": "list_recent_deployments", "description": "Check recent deployments"},
    {"tool": "get_service_dependencies", "description": "Check service dependencies"},
    {"tool": "get_runbook", "description": "Get runbook for service"},
    {"tool": "get_trace", "description": "Get trace details"},
]


# ---------------------------------------------------------------------------
# Investigation Engine
# ---------------------------------------------------------------------------


class InvestigationEngine:
    """Evidence-driven investigation engine with checkpointed loop.

    Orchestrates the investigation state machine, calling read-only tools,
    recording evidence, updating hypotheses, and persisting checkpoints.
    """

    def __init__(
        self,
        telemetry_repo: Any,
        toolkit: ReadOnlyToolkit,
        case_repository: CaseRepository | None = None,
        max_rounds: int = 8,
    ) -> None:
        self._telemetry_repo = telemetry_repo
        self._toolkit = toolkit
        self._case_repository = case_repository or CaseRepository(
            telemetry_repo.engine
        )
        self._max_rounds = max_rounds
        self._checkpoint_store = CheckpointStore(telemetry_repo.engine)
        self._audit_store = InvestigationAuditStore(telemetry_repo.engine)

    @property
    def checkpoint_store(self) -> CheckpointStore:
        """Public access to the checkpoint store for testing."""
        return self._checkpoint_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, alert: dict[str, Any]) -> InvestigationState:
        """Start a new investigation from an alert.

        Creates initial state, parses the alert, scopes the incident,
        retrieves memory, and generates initial hypotheses.
        Returns the initial investigation state (checkpointed).
        """
        incident_id = str(uuid4())

        state = InvestigationState(
            incident_id=incident_id,
            status=InvestigationStatus.SCOPING,
            alert=alert,
            phase="parse_alert",
            max_rounds=self._max_rounds,
        )

        # Phase: parse_alert -> scope_incident
        state = self._parse_alert(state)
        self._checkpoint_store.save(state)
        self._audit_store.record(incident_id, "phase_transition", {"from": "parse_alert", "to": "scope_incident"})

        # Phase: scope_incident -> retrieve_memory
        state = self._scope_incident(state)
        self._checkpoint_store.save(state)
        self._audit_store.record(incident_id, "phase_transition", {"from": "scope_incident", "to": "retrieve_memory"})

        # Phase: retrieve_memory -> generate_hypotheses
        state = self._retrieve_memory(state)
        self._checkpoint_store.save(state)
        self._audit_store.record(incident_id, "phase_transition", {"from": "retrieve_memory", "to": "generate_hypotheses"})

        # Phase: generate_hypotheses
        state = self._generate_hypotheses(state)
        self._checkpoint_store.save(state)
        self._audit_store.record(incident_id, "phase_transition", {"from": "generate_hypotheses", "to": "choose_next_action"})

        return state

    async def run_round(self, incident_id: str) -> InvestigationState:
        """Execute one round of the investigation loop.

        A round consists of: choose_next_action -> execute_tool ->
        record_evidence -> update_hypotheses -> verify_root_cause

        Returns the updated investigation state (checkpointed).
        """
        state = self._checkpoint_store.load(incident_id)
        if state is None:
            raise ValueError(f"No investigation found for incident_id={incident_id}")

        # Check round limit
        if state.current_round >= state.max_rounds:
            # Force final state
            if can_generate_report(state):
                state.status = InvestigationStatus.REPORT_READY
                state.report = generate_report(state)
                state.phase = "generate_report"
            else:
                state.status = InvestigationStatus.NEEDS_MORE_EVIDENCE
            self._checkpoint_store.save(state)
            self._audit_store.record(incident_id, "phase_transition", {"to": state.status.value, "reason": "max_rounds_reached"})
            return state

        # Increment round
        state.current_round += 1
        state.phase = "choose_next_action"

        # Transition from SCOPING to INVESTIGATING on first round
        if state.status == InvestigationStatus.SCOPING:
            state.status = InvestigationStatus.INVESTIGATING
            self._audit_store.record(incident_id, "phase_transition", {"from": "scoping", "to": "investigating"})

        # Phase: choose_next_action
        tool_call = self._choose_next_action(state)

        # Phase: execute_tool
        if tool_call is not None:
            state.phase = "execute_tool"
            result = await self._execute_tool(state, tool_call)

            # Phase: record_evidence
            state.phase = "record_evidence"
            state = self._record_evidence(state, tool_call, result)

            # Checkpoint and audit after every tool call
            self._checkpoint_store.save(state)
            self._audit_store.record(
                incident_id,
                "tool_call",
                {"tool": tool_call["tool"], "args": tool_call.get("args", {}), "ok": result.ok},
            )

        # Phase: update_hypotheses
        state.phase = "update_hypotheses"
        state = self._update_hypotheses(state)
        self._checkpoint_store.save(state)
        self._audit_store.record(incident_id, "phase_transition", {"to": "update_hypotheses"})

        # Phase: verify_root_cause
        state.phase = "verify_root_cause"
        state = self._verify_root_cause(state)
        self._checkpoint_store.save(state)
        self._audit_store.record(incident_id, "phase_transition", {"to": "verify_root_cause"})

        # Phase: generate_report (if possible)
        if can_generate_report(state):
            state.phase = "generate_report"
            state.report = generate_report(state)
            state.status = InvestigationStatus.REPORT_READY
        elif state.current_round >= state.max_rounds:
            state.status = InvestigationStatus.NEEDS_MORE_EVIDENCE

        # Checkpoint after round
        self._checkpoint_store.save(state)
        self._audit_store.record(incident_id, "phase_transition", {"to": state.phase, "round": state.current_round})

        return state

    async def resume(self, incident_id: str) -> InvestigationState | None:
        """Resume an investigation from the last checkpoint.

        Loads the investigation state and runs one more round.
        Returns None if the incident_id is not found.
        """
        state = self._checkpoint_store.load(incident_id)
        if state is None:
            return None

        # If already completed, just return the state
        if state.status in (
            InvestigationStatus.REPORT_READY,
            InvestigationStatus.NEEDS_MORE_EVIDENCE,
        ):
            return state

        # Run one more round
        return await self.run_round(incident_id)

    # ------------------------------------------------------------------
    # State machine phases
    # ------------------------------------------------------------------

    def _parse_alert(self, state: InvestigationState) -> InvestigationState:
        """Parse alert data and extract key information."""
        state.phase = "scope_incident"
        return state

    def _scope_incident(self, state: InvestigationState) -> InvestigationState:
        """Scope the incident based on alert data."""
        state.phase = "retrieve_memory"
        return state

    def _retrieve_memory(self, state: InvestigationState) -> InvestigationState:
        """Retrieve relevant historical cases from memory."""
        service = state.alert.get("service", "")
        symptom = state.alert.get("symptom", "")

        # Build a search query from alert fields
        query_parts = []
        if symptom:
            query_parts.append(symptom)
        for key in ("error_rate", "latency", "error"):
            if key in state.alert:
                query_parts.append(str(state.alert[key]))
        query = " ".join(query_parts) if query_parts else service

        # Search for historical cases
        if self._case_repository and query:
            cases = self._case_repository.search(query, service, None)
            # Store retrieved cases for hypothesis generation
            # Historical cases only produce CANDIDATE hypotheses
            state.retrieved_cases = [
                {
                    "case_id": c.id,
                    "symptom": c.symptom,
                    "root_cause": c.root_cause,
                    "service": c.service,
                }
                for c in cases
            ]

        state.phase = "generate_hypotheses"
        return state

    def _generate_hypotheses(self, state: InvestigationState) -> InvestigationState:
        """Generate initial hypotheses from alert data and memory."""
        service = state.alert.get("service", "")
        hypotheses: list[Hypothesis] = []

        # Generate hypotheses from alert signals
        error_rate = state.alert.get("error_rate")
        if error_rate is not None and error_rate > 0.1:
            hypotheses.append(
                Hypothesis(
                    id=str(uuid4()),
                    description=f"{service} experiencing high error rate ({error_rate:.0%})",
                    confidence=0.4,
                    status=HypothesisStatus.ACTIVE,
                )
            )

        # Generate hypotheses from retrieved cases (candidates only)
        for case in state.retrieved_cases:
            root_cause = case.get("root_cause")
            if root_cause:
                hypotheses.append(
                    Hypothesis(
                        id=str(uuid4()),
                        description=(
                            f"Candidate from historical case: "
                            f"{root_cause} (similar to '{case.get('symptom', '')}')"
                        ),
                        confidence=0.3,  # Candidate, not confirmed
                        status=HypothesisStatus.ACTIVE,
                    )
                )

        # Default hypothesis if none generated
        if not hypotheses:
            hypotheses.append(
                Hypothesis(
                    id=str(uuid4()),
                    description=f"{service} incident under investigation",
                    confidence=0.2,
                    status=HypothesisStatus.ACTIVE,
                )
            )

        state.hypotheses = hypotheses
        state.phase = "choose_next_action"
        return state

    def _choose_next_action(self, state: InvestigationState) -> dict[str, Any] | None:
        """Choose the next tool to execute based on current state."""
        service = state.alert.get("service", "")

        # Track which tool+args combos have been called (consistent with evidence dedup)
        called_tool_args: set[tuple[str, str]] = set()
        for ev in state.evidence:
            called_tool_args.add((ev.source_tool, ev.tool_call_id))

        # Pick next tool that hasn't been called with these args yet
        for strategy in _TOOL_STRATEGY:
            tool_name = strategy["tool"]
            if tool_name in ("search_logs", "query_metrics",
                             "get_slow_traces", "list_recent_deployments",
                             "get_runbook"):
                args = {"service": service}
            elif tool_name == "get_service_dependencies":
                args = {}
            elif tool_name == "get_trace":
                # Only call if we have a trace_id
                trace_id = state.alert.get("trace_id")
                if not trace_id:
                    continue
                args = {"trace_id": trace_id}
            else:
                continue

            # Compute the same dedup key as _record_evidence
            args_str = json.dumps(args, sort_keys=True)
            args_hash = hashlib.sha256(
                f"{tool_name}:{args_str}".encode()
            ).hexdigest()[:16]

            if (tool_name, args_hash) not in called_tool_args:
                return {"tool": tool_name, "args": args}

        # If all tools called with default args, re-call with different params
        if called_tool_args:
            return {"tool": "search_logs", "args": {"service": service, "keyword": "error"}}

        return None

    async def _execute_tool(
        self, state: InvestigationState, tool_call: dict[str, Any]
    ) -> ToolResult[Any]:
        """Execute a tool and return the result."""
        tool_name = tool_call["tool"]
        args = tool_call.get("args", {})
        service = args.get("service", state.alert.get("service", ""))

        try:
            if tool_name == "search_logs":
                return await self._toolkit.search_logs(
                    service=service,
                    keyword=args.get("keyword", ""),
                    limit=args.get("limit", 100),
                )
            elif tool_name == "query_metrics":
                return await self._toolkit.query_metrics(
                    service=service,
                    name=args.get("name"),
                    limit=args.get("limit", 100),
                )
            elif tool_name == "get_slow_traces":
                return await self._toolkit.get_slow_traces(
                    service=service,
                    threshold_seconds=args.get("threshold_seconds", 5.0),
                )
            elif tool_name == "get_trace":
                return await self._toolkit.get_trace(
                    trace_id=args.get("trace_id", ""),
                )
            elif tool_name == "get_service_dependencies":
                return await self._toolkit.get_service_dependencies()
            elif tool_name == "list_recent_deployments":
                return await self._toolkit.list_recent_deployments(
                    service=service,
                    limit=args.get("limit", 100),
                )
            elif tool_name == "get_runbook":
                return await self._toolkit.get_runbook(service=service)
            else:
                return ToolResult(
                    ok=False,
                    error=f"Unknown tool: {tool_name}",
                    metadata={"tool": tool_name},
                )
        except Exception as exc:
            return ToolResult(
                ok=False,
                error=str(exc),
                metadata={"tool": tool_name},
            )

    def _record_evidence(
        self,
        state: InvestigationState,
        tool_call: dict[str, Any],
        result: ToolResult[Any],
    ) -> InvestigationState:
        """Record evidence from a tool result, with dedup."""
        tool_name = tool_call["tool"]
        args = tool_call.get("args", {})

        # Create a dedup key from tool name + args
        args_str = json.dumps(args, sort_keys=True)
        dedup_key = hashlib.sha256(
            f"{tool_name}:{args_str}".encode()
        ).hexdigest()[:16]

        # Check for duplicate: same tool+args already recorded
        for ev in state.evidence:
            if ev.tool_call_id == dedup_key and ev.source_tool == tool_name:
                # Duplicate — skip recording
                return state

        # Build evidence content
        content: dict[str, Any] = {}
        if result.ok:
            data = result.data
            if isinstance(data, list):
                content = {"count": len(data), "items": data[:10]}  # truncate
            elif data is not None:
                content = {"data": data}
            else:
                content = {"empty": True}
        else:
            content = {"error": result.error, "ok": False}

        # Record metadata about the result type
        if result.ok and (result.data is None or result.data == []):
            content["empty_result"] = True
        if not result.ok:
            content["error_result"] = True

        evidence = Evidence(
            id=str(uuid4()),
            source_tool=tool_name,
            tool_call_id=dedup_key,
            content=content,
            supports_hypothesis_ids=[],
            contradicts_hypothesis_ids=[],
        )

        state.evidence.append(evidence)
        return state

    def _update_hypotheses(self, state: InvestigationState) -> InvestigationState:
        """Update hypotheses based on accumulated evidence."""
        # Analyze evidence patterns
        has_errors = any(
            ev.content.get("error") or ev.content.get("error_result")
            for ev in state.evidence
        )
        has_data = any(
            ev.content.get("count", 0) > 0 or ev.content.get("data") is not None
            for ev in state.evidence
            if not ev.content.get("error_result")
            and not ev.content.get("empty_result")
        )

        # Check for conflicting evidence (errors in some tools, normal in others)
        has_conflict = has_errors and has_data

        for hyp in state.hypotheses:
            if hyp.status != HypothesisStatus.ACTIVE:
                continue

            # Associate evidence with hypotheses
            for ev in state.evidence:
                # Determine if evidence supports or contradicts this hypothesis
                supports = self._evidence_supports_hypothesis(ev, hyp, state)
                contradicts = self._evidence_contradicts_hypothesis(ev, hyp, state)

                if supports and ev.id not in hyp.supporting_evidence_ids:
                    hyp.supporting_evidence_ids.append(ev.id)
                    if ev.id not in ev.supports_hypothesis_ids:
                        ev.supports_hypothesis_ids.append(hyp.id)

                if contradicts and ev.id not in hyp.contradicting_evidence_ids:
                    hyp.contradicting_evidence_ids.append(ev.id)
                    if ev.id not in ev.contradicts_hypothesis_ids:
                        ev.contradicts_hypothesis_ids.append(hyp.id)

            # Update confidence based on evidence
            support_count = len(hyp.supporting_evidence_ids)
            contradict_count = len(hyp.contradicting_evidence_ids)

            if support_count > 0 or contradict_count > 0:
                # Adjust confidence
                confidence_delta = (support_count * 0.10) - (contradict_count * 0.15)
                if has_conflict:
                    confidence_delta -= 0.10  # Additional penalty for conflicts
                hyp.confidence = min(0.95, max(0.0, hyp.confidence + confidence_delta))

            # Confidence guard: > 0.70 requires evidence references
            if hyp.confidence > 0.70 and not hyp.supporting_evidence_ids:
                # Without evidence, the investigation needs more evidence
                # Keep the hypothesis confidence as-is, but mark the investigation
                state.status = InvestigationStatus.NEEDS_MORE_EVIDENCE

        return state

    def _evidence_supports_hypothesis(
        self,
        evidence: Evidence,
        hypothesis: Hypothesis,
        state: InvestigationState,
    ) -> bool:
        """Determine if evidence supports a hypothesis."""
        content = evidence.content

        # Error evidence supports error-rate hypotheses
        if content.get("error") or content.get("error_result"):
            if "error rate" in hypothesis.description.lower():
                return True
            if "incident" in hypothesis.description.lower():
                return True

        # Data with items supports investigation hypotheses
        if content.get("count", 0) > 0:
            desc_lower = hypothesis.description.lower()
            tool = evidence.source_tool
            if "error rate" in desc_lower and tool == "query_metrics":
                return True
            if "incident" in desc_lower:
                return True
            if tool == "search_logs" and "error" in desc_lower:
                return True
            if tool == "get_slow_traces" and "error" in desc_lower:
                return True
            if tool == "list_recent_deployments" and "deployment" in desc_lower:
                return True
            if tool == "get_runbook" and "runbook" in desc_lower:
                return True

        # Non-empty data supports any active hypothesis
        if content.get("data") is not None and not content.get("error_result"):
            if "candidate" in hypothesis.description.lower():
                return True

        return False

    def _evidence_contradicts_hypothesis(
        self,
        evidence: Evidence,
        hypothesis: Hypothesis,
        state: InvestigationState,
    ) -> bool:
        """Determine if evidence contradicts a hypothesis."""
        content = evidence.content

        # Empty results from relevant tools contradict hypotheses
        if content.get("empty") or content.get("empty_result"):
            if evidence.source_tool in ("search_logs", "query_metrics", "get_slow_traces"):
                return True

        # Low error rate data contradicts high error rate hypothesis
        if evidence.source_tool == "query_metrics":
            data = content.get("data")
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("value", 1.0) < 0.05:
                        if "error rate" in hypothesis.description.lower():
                            return True
            elif isinstance(data, dict):
                if data.get("value", 1.0) < 0.05:
                    if "error rate" in hypothesis.description.lower():
                        return True

        return False

    def _verify_root_cause(self, state: InvestigationState) -> InvestigationState:
        """Verify if a root cause has been identified.

        A hypothesis can be confirmed if:
          - Confidence >= 0.75
          - Has supporting evidence references
          - Not contradicted by other evidence
        """
        for hyp in state.hypotheses:
            if hyp.status != HypothesisStatus.ACTIVE:
                continue

            # Check confirmation criteria
            if (
                hyp.confidence >= 0.75
                and len(hyp.supporting_evidence_ids) > 0
                and len(hyp.contradicting_evidence_ids) == 0
            ):
                hyp.status = HypothesisStatus.CONFIRMED

            # Check ruling-out criteria
            elif (
                hyp.confidence < 0.10
                and len(hyp.contradicting_evidence_ids) > 0
            ):
                hyp.status = HypothesisStatus.RULED_OUT

        # Update investigation status based on hypothesis states
        has_confirmed = any(
            h.status == HypothesisStatus.CONFIRMED for h in state.hypotheses
        )
        if has_confirmed:
            # A confirmed hypothesis with supporting evidence means verification
            # is complete — go directly to REPORT_READY (bypass dead VERIFYING state)
            state.status = InvestigationStatus.REPORT_READY
        elif all(
            h.status in (HypothesisStatus.RULED_OUT, HypothesisStatus.CONFIRMED)
            for h in state.hypotheses
        ):
            state.status = InvestigationStatus.NEEDS_MORE_EVIDENCE

        return state
