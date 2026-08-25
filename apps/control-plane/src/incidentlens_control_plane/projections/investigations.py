"""Read-only investigation summary projection."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Callable

from pydantic import BaseModel, ConfigDict

from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.changes.store import ChangeSetStore
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEventType
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.types import Conclusion
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.projections.issues import (
    ChangeSummaryView,
    EvidenceSnippetView,
    VerificationSummaryView,
    _changeset_summary,
    _evidence_snippets,
    _issue_id,
    _matching_changes,
    _matching_evidence,
    _matching_pending_approvals,
    _target_lookup,
    _validation_summary,
)
from incidentlens_control_plane.targets.service import TargetService
from incidentlens_control_plane.targets.store import TargetStore


class InvestigationMilestoneView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    event_type: RuntimeEventType
    occurred_at: datetime
    status: str | None = None
    summary: str | None = None


class HypothesisSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str
    status: str
    summary: str
    updated_at: datetime


class ConclusionSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str
    evidence_ids: tuple[str, ...]


class InvestigationSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: str
    issue_id: str
    target_id: str
    service_id: str
    symptom: str
    status: str
    pending_approval_ids: tuple[str, ...] = ()
    milestones: tuple[InvestigationMilestoneView, ...] = ()
    hypotheses: tuple[HypothesisSummaryView, ...] = ()
    evidence: tuple[EvidenceSnippetView, ...] = ()
    conclusion: ConclusionSummaryView | None = None
    change_summaries: tuple[ChangeSummaryView, ...] = ()
    verification_summaries: tuple[VerificationSummaryView, ...] = ()
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class InvestigationSummaryPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[InvestigationSummaryView, ...]
    next_cursor: str | None = None
    has_more: bool


_MILESTONE_EVENT_TYPES = (
    RuntimeEventType.INVESTIGATION_CREATED,
    RuntimeEventType.INVESTIGATION_STARTED,
    RuntimeEventType.INVESTIGATION_STATUS_CHANGED,
    RuntimeEventType.INVESTIGATION_COMPLETED,
    RuntimeEventType.INVESTIGATION_CANCELLED,
    RuntimeEventType.INVESTIGATION_FAILED,
    RuntimeEventType.APPROVAL_REQUESTED,
    RuntimeEventType.APPROVAL_APPROVED,
    RuntimeEventType.APPROVAL_REJECTED,
    RuntimeEventType.APPROVAL_CONSUMED,
    RuntimeEventType.CHANGESET_CREATED,
    RuntimeEventType.CHANGESET_STATUS_CHANGED,
    RuntimeEventType.CHANGESET_ROLLED_BACK,
    RuntimeEventType.CONCLUSION_CREATED,
    RuntimeEventType.REGISTRY_PROPOSAL_CREATED,
    RuntimeEventType.REGISTRY_PROPOSAL_DECIDED,
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("projection timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _encode_cursor(created_at: datetime, investigation_id: str) -> str:
    raw = json.dumps(
        {
            "created_at": created_at.astimezone(UTC).isoformat(),
            "investigation_id": investigation_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "iv1_" + base64.urlsafe_b64encode(raw).decode("ascii")


def decode_investigation_cursor(value: str | None) -> tuple[datetime, str] | None:
    if value is None:
        return None
    if not value.startswith("iv1_"):
        raise ValueError("investigation cursor is invalid")
    try:
        payload = base64.urlsafe_b64decode(value[4:].encode("ascii")).decode("utf-8")
        body = json.loads(payload)
        created_at = datetime.fromisoformat(str(body["created_at"]))
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("investigation cursor is invalid")
        return created_at.astimezone(UTC), str(body["investigation_id"])
    except Exception as exc:  # noqa: BLE001
        raise ValueError("investigation cursor is invalid") from exc


def _conclusion(store: InvestigationStore, investigation_id: str) -> Conclusion | None:
    grounded = [
        conclusion
        for conclusion in store.list_conclusions(investigation_id=investigation_id)
        if conclusion.evidence_ids
    ]
    return grounded[-1] if grounded else None


def _milestones(
    events: RuntimeEventStore,
    *,
    investigation_id: str,
) -> tuple[InvestigationMilestoneView, ...]:
    items = []
    after_sequence = 0
    while True:
        page = events.list_page(
            after_sequence=after_sequence,
            limit=500,
            investigation_id=investigation_id,
            event_types=_MILESTONE_EVENT_TYPES,
        )
        for event in page.items:
            summary = None
            if event.event_type is RuntimeEventType.CONCLUSION_CREATED:
                conclusion = event.payload.get("conclusion")
                if isinstance(conclusion, dict):
                    text = conclusion.get("summary")
                    summary = str(text) if text is not None else None
            items.append(
                InvestigationMilestoneView(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    occurred_at=event.occurred_at,
                    status=(
                        str(event.payload["status"])
                        if event.payload.get("status") is not None
                        else None
                    ),
                    summary=summary,
                )
            )
        if not page.has_more or not page.items:
            break
        after_sequence = page.next_after_sequence
    return tuple(items)


class InvestigationSummaryProjectionService:
    def __init__(
        self,
        *,
        target_service: TargetService,
        target_store: TargetStore,
        investigations: InvestigationStore,
        approvals: ApprovalStore,
        changes: ChangeSetStore,
        evidence: EvidenceStore,
        logs: LogStore,
        events: RuntimeEventStore,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._target_service = target_service
        self._target_store = target_store
        self._investigations = investigations
        self._approvals = approvals
        self._changes = changes
        self._evidence = evidence
        self._logs = logs
        self._events = events
        self._now = now or (lambda: datetime.now(UTC))

    def list_summaries(
        self,
        *,
        allowed_target_ids: frozenset[str] | None = None,
        target_id: str | None = None,
        service_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        after: tuple[datetime, str] | None = None,
    ) -> InvestigationSummaryPage:
        if not (1 <= limit <= 500):
            raise ValueError("limit must be between 1 and 500")
        items = list(
            self._all_summaries(
                allowed_target_ids=allowed_target_ids,
                target_id=target_id,
                service_id=service_id,
            )
        )
        if status is not None:
            items = [item for item in items if item.status == status]
        items.sort(key=lambda item: (item.created_at, item.investigation_id))
        if after is not None:
            after_created_at = _utc(after[0])
            items = [
                item
                for item in items
                if item.created_at > after_created_at
                or (
                    item.created_at == after_created_at
                    and item.investigation_id > after[1]
                )
            ]
        has_more = len(items) > limit
        selected = tuple(items[:limit])
        next_cursor = (
            _encode_cursor(selected[-1].created_at, selected[-1].investigation_id)
            if selected and has_more
            else None
        )
        return InvestigationSummaryPage(
            items=selected,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def get_summary(
        self,
        investigation_id: str,
        *,
        allowed_target_ids: frozenset[str] | None = None,
    ) -> InvestigationSummaryView | None:
        investigation = next(
            (
                record
                for record in self._investigations.list_investigations()
                if record.investigation_id == investigation_id
            ),
            None,
        )
        if investigation is None:
            return None
        generated_at = _utc(self._now())
        targets = _target_lookup(self._target_service, self._target_store, now=generated_at)
        facade_target_id = targets.get((investigation.project_id, investigation.target_id))
        if facade_target_id is None:
            return None
        if allowed_target_ids is not None and facade_target_id not in allowed_target_ids:
            return None
        return self._build_summary(investigation, facade_target_id=facade_target_id)

    def _all_summaries(
        self,
        *,
        allowed_target_ids: frozenset[str] | None,
        target_id: str | None,
        service_id: str | None,
    ) -> tuple[InvestigationSummaryView, ...]:
        generated_at = _utc(self._now())
        targets = _target_lookup(self._target_service, self._target_store, now=generated_at)
        items: list[InvestigationSummaryView] = []
        for investigation in self._investigations.list_investigations():
            facade_target_id = targets.get((investigation.project_id, investigation.target_id))
            if facade_target_id is None:
                continue
            if allowed_target_ids is not None and facade_target_id not in allowed_target_ids:
                continue
            if target_id is not None and target_id != facade_target_id:
                continue
            if service_id is not None and service_id != investigation.service:
                continue
            items.append(
                self._build_summary(investigation, facade_target_id=facade_target_id)
            )
        return tuple(items)

    def _build_summary(self, investigation, *, facade_target_id: str) -> InvestigationSummaryView:
        evidence_refs = _matching_evidence(
            self._evidence,
            incident_id=investigation.incident_id,
            project_id=investigation.project_id,
            registry_target_id=investigation.target_id,
            service_name=investigation.service,
        )
        change_sets = _matching_changes(
            self._changes,
            incident_id=investigation.incident_id,
            project_id=investigation.project_id,
            registry_target_id=investigation.target_id,
            service_name=investigation.service,
        )
        validations = tuple(
            ref for ref in evidence_refs if ref.evidence_kind.value == "validation_result"
        )
        hypothesis_views = tuple(
            HypothesisSummaryView(
                hypothesis_id=hypothesis.hypothesis_id,
                status=hypothesis.status.value,
                summary=hypothesis.summary,
                updated_at=hypothesis.updated_at,
            )
            for hypothesis in self._investigations.list_hypotheses(
                investigation_id=investigation.investigation_id
            )
        )
        conclusion = _conclusion(self._investigations, investigation.investigation_id)
        return InvestigationSummaryView(
            investigation_id=investigation.investigation_id,
            issue_id=_issue_id(investigation.investigation_id),
            target_id=facade_target_id,
            service_id=investigation.service,
            symptom=investigation.symptom,
            status=investigation.status.value,
            pending_approval_ids=_matching_pending_approvals(
                self._approvals,
                investigation_id=investigation.investigation_id,
                facade_target_id=facade_target_id,
                registry_target_id=investigation.target_id,
                service_name=investigation.service,
            ),
            milestones=_milestones(
                self._events,
                investigation_id=investigation.investigation_id,
            ),
            hypotheses=hypothesis_views,
            evidence=_evidence_snippets(
                evidence_refs,
                facade_target_id=facade_target_id,
                logs=self._logs,
            ),
            conclusion=(
                ConclusionSummaryView(
                    summary=conclusion.summary,
                    evidence_ids=conclusion.evidence_ids,
                )
                if conclusion is not None
                else None
            ),
            change_summaries=tuple(_changeset_summary(changeset) for changeset in change_sets),
            verification_summaries=tuple(
                _validation_summary(evidence) for evidence in validations
            ),
            created_at=investigation.created_at,
            updated_at=investigation.updated_at,
            started_at=investigation.started_at,
            completed_at=investigation.completed_at,
        )


__all__ = [
    "InvestigationSummaryPage",
    "InvestigationSummaryProjectionService",
    "InvestigationSummaryView",
    "decode_investigation_cursor",
]
