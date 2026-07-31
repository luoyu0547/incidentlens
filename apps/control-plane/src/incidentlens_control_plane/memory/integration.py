"""InvestigationMemoryCoordinator — connects agent runtimes to governed memory.

Provides deterministic preparation (case recall + candidate hypotheses) and
terminal reconciliation (materialization + validated/misleading classification).

All usage events are idempotent by ``idempotency_key`` so that retries,
duplicate SSE events, and repeated terminal operations never produce
duplicate rows.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from incidentlens_contracts.models import (
    Hypothesis,
    HypothesisStatus,
    InvestigationStatus,
)

from incidentlens_control_plane.agent.state import (
    InvestigationAuditStore,
    InvestigationState,
)
from incidentlens_control_plane.memory.domain import (
    CaseSearchQuery,
    CaseUsageEvent,
    UsageEventType,
)
from incidentlens_control_plane.memory.models import CaseUsageEventRow
from incidentlens_control_plane.memory.repository import CaseRepository
from incidentlens_control_plane.memory.retrieval import HybridCaseRetriever
from incidentlens_control_plane.memory.service import CaseService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deterministic hypothesis identity
# ---------------------------------------------------------------------------


def historical_hypothesis_id(incident_id: str, case_id: int) -> str:
    """Deterministic UUID5 so retries recreate the same candidate identity."""
    return str(uuid5(NAMESPACE_URL, f"incidentlens:{incident_id}:case:{case_id}"))


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


@dataclass
class MemoryPreparation:
    """Result of coordinator.prepare(): retrieved cases and candidate hypotheses."""

    retrieved_cases: list[dict[str, Any]]
    hypotheses: list[Hypothesis]


# ---------------------------------------------------------------------------
# InvestigationMemoryCoordinator
# ---------------------------------------------------------------------------


class InvestigationMemoryCoordinator:
    """Bridges agent runtimes to governed case memory.

    Responsibilities:
      - prepare(): search historical cases, record recalled/adopted events,
        create candidate hypotheses with deterministic IDs.
      - finalize(): for terminal report_ready states, materialize a case,
        classify each prior as validated or misleading, record terminal events.
      - usage_counts() / list_usage(): observability into usage events.
    """

    def __init__(
        self,
        *,
        retriever: HybridCaseRetriever,
        case_service: CaseService,
        repository: CaseRepository,
        audit_store: InvestigationAuditStore,
    ) -> None:
        self._retriever = retriever
        self._case_service = case_service
        self._repository = repository
        self._audit_store = audit_store
        # In-memory event buffer for querying during the session
        self._events: dict[str, list[CaseUsageEvent]] = {}

    # ------------------------------------------------------------------
    # prepare — called at investigation start
    # ------------------------------------------------------------------

    def prepare(
        self,
        incident_id: str,
        alert: dict[str, Any],
    ) -> MemoryPreparation:
        """Search historical cases, record usage events, create candidate hypotheses.

        Returns a ``MemoryPreparation`` with enriched retrieved_cases (each
        carrying a ``hypothesis_id``) and ACTIVE hypotheses at confidence 0.3.
        """
        service = alert.get("service", "")
        symptom = alert.get("symptom", "")

        # Build search query
        query_parts: list[str] = []
        if symptom:
            query_parts.append(symptom)
        for key in ("error_rate", "latency", "error"):
            if key in alert:
                query_parts.append(str(alert[key]))
        text = " ".join(query_parts) if query_parts else service

        if not text:
            return MemoryPreparation(retrieved_cases=[], hypotheses=[])

        search_query = CaseSearchQuery(text=text, service=service or None)
        hits = self._retriever.search(search_query)

        # Record degradation reason if any
        if self._retriever.last_degradation_reason:
            self._audit_store.record(
                incident_id,
                "memory_retrieval_degraded",
                {
                    "reason_code": self._retriever.last_degradation_reason,
                    "retrieval_mode": self._retriever.search.__func__.__qualname__
                    if hasattr(self._retriever.search, "__func__")
                    else "hybrid",
                },
            )

        retrieved_cases: list[dict[str, Any]] = []
        hypotheses: list[Hypothesis] = []

        for rank, hit in enumerate(hits, start=1):
            case_id = hit.case_id
            hyp_id = historical_hypothesis_id(incident_id, case_id)

            # Record recalled event
            self._record_usage_event(
                CaseUsageEventRow(
                    case_id=case_id,
                    hypothesis_id=hyp_id,
                    event_type=UsageEventType.RECALLED,
                    idempotency_key=f"{incident_id}:{case_id}:recalled",
                    investigation_id=incident_id,
                    details_json=json.dumps({}),
                ),
                incident_id,
                CaseUsageEvent(
                    id=0,
                    case_id=case_id,
                    incident_id=incident_id,
                    hypothesis_id=hyp_id,
                    event_type=UsageEventType.RECALLED,
                    idempotency_key=f"{incident_id}:{case_id}:recalled",
                    rank=rank,
                    retrieval_mode=hit.retrieval_mode,
                    lexical_score=hit.lexical_score,
                    semantic_score=hit.semantic_score,
                    filter_score=hit.filter_score,
                    similarity_reason=hit.similarity_reason,
                ),
            )

            # Create candidate hypothesis
            snapshot = hit.case_snapshot
            root_cause = snapshot.root_cause_category
            hypothesis = Hypothesis(
                id=hyp_id,
                description=(
                    f"Candidate from historical case {case_id}: "
                    f"{root_cause} ({hit.similarity_reason})"
                ),
                confidence=0.3,
                status=HypothesisStatus.ACTIVE,
                root_service=service or (
                    snapshot.affected_services[0]
                    if snapshot.affected_services
                    else ""
                ),
                cause_code=root_cause,
            )
            hypotheses.append(hypothesis)

            # Record adopted event
            self._record_usage_event(
                CaseUsageEventRow(
                    case_id=case_id,
                    hypothesis_id=hyp_id,
                    event_type=UsageEventType.ADOPTED,
                    idempotency_key=f"{incident_id}:{case_id}:{hyp_id}:adopted",
                    investigation_id=incident_id,
                    details_json=json.dumps({}),
                ),
                incident_id,
                CaseUsageEvent(
                    id=0,
                    case_id=case_id,
                    incident_id=incident_id,
                    hypothesis_id=hyp_id,
                    event_type=UsageEventType.ADOPTED,
                    idempotency_key=f"{incident_id}:{case_id}:{hyp_id}:adopted",
                    rank=rank,
                    retrieval_mode=hit.retrieval_mode,
                ),
            )

            retrieved_cases.append(
                {
                    "case_id": case_id,
                    "root_cause_category": root_cause,
                    "hypothesis_id": hyp_id,
                    "symptom": snapshot.symptom,
                    "root_cause": root_cause,
                    "service": service,
                }
            )

        return MemoryPreparation(
            retrieved_cases=retrieved_cases,
            hypotheses=hypotheses,
        )

    # ------------------------------------------------------------------
    # finalize — called on terminal states
    # ------------------------------------------------------------------

    def finalize(self, state: InvestigationState) -> InvestigationState:
        """Reconcile terminal state with case memory.

        For non-terminal or needs_more_evidence states, returns unchanged.
        For report_ready: materializes a case, classifies priors, and
        records terminal usage events.

        Materialization failures are caught, logged to the audit store,
        and the original state is returned unchanged so a later retry
        can succeed.
        """
        # Guard: only process terminal states
        if state.status not in (
            InvestigationStatus.REPORT_READY,
            InvestigationStatus.NEEDS_MORE_EVIDENCE,
        ):
            return state

        # Guard: needs_more_evidence does not trigger materialization
        if state.status == InvestigationStatus.NEEDS_MORE_EVIDENCE:
            return state

        # --- report_ready path ---
        try:
            snapshot = self._case_service.materialize_from_investigation(state)
            state.case_id = snapshot.id
            state.case_status = "agent_generated"
        except Exception:
            self._audit_store.record(
                state.incident_id,
                "case_materialization_failed",
                {"error_code": "case_storage_error"},
            )
            return state

        # Classify each adopted prior against the report root cause
        report_root_cause = (
            state.report.get("root_cause", "") if state.report else ""
        )

        for case_dict in state.retrieved_cases:
            case_id = case_dict.get("case_id")
            case_category = case_dict.get("root_cause_category", "")
            hyp_id = case_dict.get("hypothesis_id", "")

            if case_id is None:
                continue

            evidence_ids = (
                state.report.get("evidence_ids", []) if state.report else []
            )

            if case_category == report_root_cause:
                event_type = UsageEventType.VALIDATED
            else:
                event_type = UsageEventType.MISLEADING

            details: dict[str, Any] = {}
            if event_type == UsageEventType.MISLEADING:
                details["accepted_evidence_ids"] = evidence_ids

            self._record_usage_event(
                CaseUsageEventRow(
                    case_id=case_id,
                    hypothesis_id=hyp_id,
                    event_type=event_type,
                    idempotency_key=(
                        f"{state.incident_id}:{case_id}:{hyp_id}:{event_type.value}"
                    ),
                    investigation_id=state.incident_id,
                    details_json=json.dumps(details),
                ),
                state.incident_id,
                CaseUsageEvent(
                    id=0,
                    case_id=case_id,
                    incident_id=state.incident_id,
                    hypothesis_id=hyp_id,
                    event_type=event_type,
                    idempotency_key=(
                        f"{state.incident_id}:{case_id}:{hyp_id}:{event_type.value}"
                    ),
                    details=details,
                ),
            )

        return state

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def usage_counts(self, incident_id: str) -> dict[str, int]:
        """Return event type counts for an incident."""
        events = self._events.get(incident_id, [])
        counts: dict[str, int] = {
            "recalled": 0,
            "adopted": 0,
            "validated": 0,
            "misleading": 0,
        }
        for event in events:
            key = event.event_type.value
            if key in counts:
                counts[key] += 1
        return counts

    def list_usage(self, incident_id: str) -> list[CaseUsageEvent]:
        """Return all usage events for an incident."""
        return list(self._events.get(incident_id, []))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record_usage_event(
        self,
        row: CaseUsageEventRow,
        incident_id: str,
        memory_event: CaseUsageEvent,
    ) -> None:
        """Record a usage event to the repository and in-memory buffer.

        Uses idempotency_key uniqueness to prevent duplicates. On
        IntegrityError (duplicate key), the existing row is preserved.
        """
        try:
            with self._repository.transaction() as session:
                self._repository.add_usage_event(session, row)
        except Exception:
            # Duplicate idempotency_key — expected on retries
            pass

        # Buffer for in-memory queries
        if incident_id not in self._events:
            self._events[incident_id] = []
        # Deduplicate by idempotency_key
        existing_keys = {
            e.idempotency_key for e in self._events[incident_id]
        }
        if memory_event.idempotency_key not in existing_keys:
            self._events[incident_id].append(memory_event)
