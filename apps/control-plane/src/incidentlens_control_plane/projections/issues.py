"""Read-only issue projection over investigations, evidence, and changes."""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.approvals.types import ApprovalStatus
from incidentlens_control_plane.changes.store import ChangeSetStore
from incidentlens_control_plane.changes.types import ChangeSet, ChangeSetStatus
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.evidence.types import EvidenceKind, EvidenceRef
from incidentlens_control_plane.investigation.state_machine import InvestigationStatus
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.logs.cursors import encode_log_cursor
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.logs.types import LogScope, LogSeverity, LogSourceKind
from incidentlens_control_plane.targets.service import TargetService
from incidentlens_control_plane.targets.store import TargetStore

_EXPLICIT_SUCCESS = frozenset({"true", "passed", "success", "succeeded", "verified"})


class IssueStatus(StrEnum):
    OPEN = "open"
    WAITING_APPROVAL = "waiting_approval"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChangeSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    changeset_id: str
    status: ChangeSetStatus
    file_count: int = Field(ge=0)
    scopes: tuple[str, ...]
    approval_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class VerificationSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_ref_id: str
    passed: bool | None = None
    validator: str | None = None
    summary: str
    created_at: datetime


class EvidenceSnippetView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_ref_id: str
    evidence_kind: EvidenceKind
    target_id: str
    service_id: str
    source_kind: LogSourceKind | None = None
    scope: LogScope | None = None
    severity: LogSeverity | None = None
    log_cursor: str | None = None
    summary: str
    created_at: datetime


class IssueView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_id: str
    investigation_id: str
    target_id: str
    service_id: str
    symptom: str
    status: IssueStatus
    severity: LogSeverity | None = None
    root_cause: str | None = None
    root_cause_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    pending_approval_ids: tuple[str, ...] = ()
    evidence: tuple[EvidenceSnippetView, ...] = ()
    resolution: ChangeSummaryView | None = None
    verification: VerificationSummaryView | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class IssuePage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[IssueView, ...]
    next_cursor: str | None = None
    has_more: bool


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("projection timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _issue_id(investigation_id: str) -> str:
    return f"iss_{investigation_id}"


def _encode_cursor(created_at: datetime, issue_id: str) -> str:
    raw = json.dumps(
        {
            "created_at": created_at.astimezone(UTC).isoformat(),
            "issue_id": issue_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "is1_" + base64.urlsafe_b64encode(raw).decode("ascii")


def decode_issue_cursor(value: str | None) -> tuple[datetime, str] | None:
    if value is None:
        return None
    if not value.startswith("is1_"):
        raise ValueError("issue cursor is invalid")
    try:
        payload = base64.urlsafe_b64decode(value[4:].encode("ascii")).decode("utf-8")
        body = json.loads(payload)
        created_at = datetime.fromisoformat(str(body["created_at"]))
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("issue cursor is invalid")
        return created_at.astimezone(UTC), str(body["issue_id"])
    except Exception as exc:  # noqa: BLE001
        raise ValueError("issue cursor is invalid") from exc


def _severity_rank(severity: LogSeverity | None) -> int:
    order = {
        LogSeverity.TRACE: 0,
        LogSeverity.DEBUG: 1,
        LogSeverity.INFO: 2,
        LogSeverity.NOTICE: 3,
        LogSeverity.WARN: 4,
        LogSeverity.ERROR: 5,
        LogSeverity.CRITICAL: 6,
        LogSeverity.UNKNOWN: -1,
        None: -1,
    }
    return order[severity]


def _changeset_summary(changeset: ChangeSet) -> ChangeSummaryView:
    scopes = tuple(dict.fromkeys(file_change.scope for file_change in changeset.files))
    return ChangeSummaryView(
        changeset_id=changeset.changeset_id,
        status=changeset.status,
        file_count=len(changeset.files),
        scopes=scopes,
        approval_id=changeset.approval_id,
        created_at=changeset.created_at,
        updated_at=changeset.updated_at,
    )


def _validation_summary(evidence: EvidenceRef) -> VerificationSummaryView:
    passed = _validation_passed(evidence)
    return VerificationSummaryView(
        evidence_ref_id=evidence.evidence_ref_id,
        passed=passed,
        validator=evidence.metadata.get("validator"),
        summary=evidence.content_redacted,
        created_at=evidence.created_at,
    )


def _validation_passed(evidence: EvidenceRef) -> bool | None:
    passed_value = evidence.metadata.get("passed")
    if passed_value is None:
        return None
    normalized = passed_value.strip().lower()
    if normalized in _EXPLICIT_SUCCESS:
        return True
    if normalized in {"false", "failed", "failure", "error", "unsuccessful"}:
        return False
    return None


def _status_for(
    investigation_status: InvestigationStatus,
    latest_change: ChangeSet | None,
    latest_verification: EvidenceRef | None,
) -> IssueStatus:
    if investigation_status is InvestigationStatus.CANCELLED:
        return IssueStatus.CANCELLED
    if investigation_status in {
        InvestigationStatus.FAILED,
        InvestigationStatus.PAUSED_UNCERTAIN_STATE,
    }:
        return IssueStatus.FAILED
    if investigation_status is InvestigationStatus.WAITING_APPROVAL:
        return IssueStatus.WAITING_APPROVAL
    if investigation_status is InvestigationStatus.COMPLETED:
        return IssueStatus.RESOLVED
    if latest_change is not None and latest_change.status in {
        ChangeSetStatus.APPLIED,
        ChangeSetStatus.VALIDATED,
        ChangeSetStatus.VERIFIED,
    }:
        return (
            IssueStatus.RESOLVED
            if latest_verification is not None
            and _validation_passed(latest_verification) is True
            else IssueStatus.MITIGATED
        )
    return IssueStatus.OPEN


def _root_cause(store: InvestigationStore, investigation_id: str) -> str | None:
    conclusions = store.list_conclusions(investigation_id=investigation_id)
    grounded = [conclusion for conclusion in conclusions if conclusion.evidence_ids]
    return grounded[-1].summary if grounded else None


def _target_lookup(
    target_service: TargetService,
    target_store: TargetStore,
    *,
    now: datetime,
) -> dict[tuple[str, str], str]:
    lookup: dict[tuple[str, str], str] = {}
    for target in target_service.list_targets(now=now):
        binding = target_store.get(target.target_id)
        lookup[(binding.project_id, binding.registry_target_id)] = target.target_id
    return lookup


def _matching_changes(
    changes: ChangeSetStore,
    *,
    incident_id: str,
    project_id: str,
    registry_target_id: str,
    service_name: str,
) -> tuple[ChangeSet, ...]:
    return tuple(
        changeset
        for changeset in changes.list_for_incident(incident_id, limit=200)
        if changeset.project_id == project_id
        and changeset.target_id == registry_target_id
        and changeset.service_name == service_name
    )


def _matching_evidence(
    evidence: EvidenceStore,
    *,
    incident_id: str,
    project_id: str,
    registry_target_id: str,
    service_name: str,
) -> tuple[EvidenceRef, ...]:
    return tuple(
        ref
        for ref in evidence.list_for_incident(incident_id, limit=500)
        if ref.project_id == project_id
        and ref.target_id == registry_target_id
        and ref.service_name == service_name
    )


def _matching_pending_approvals(
    approvals: ApprovalStore,
    *,
    investigation_id: str,
    facade_target_id: str,
    registry_target_id: str,
    service_name: str,
) -> tuple[str, ...]:
    return tuple(
        record.approval_id
        for record in approvals.list(
            ApprovalStatus.PENDING,
            investigation_id=investigation_id,
        )
        if record.service in {None, service_name}
        and record.target_id in {None, facade_target_id, registry_target_id}
    )


def _log_cursor(logs: LogStore, evidence: EvidenceRef) -> str | None:
    if evidence.evidence_kind is not EvidenceKind.LOG_RECORD:
        return None
    connection_factory: Callable[[], sqlite3.Connection] = logs._connection_factory
    with connection_factory() as conn:
        row = conn.execute(
            """
            SELECT stream_sequence
            FROM log_records
            WHERE project_id = ?
              AND target_id = ?
              AND service_name = ?
              AND source_kind = ?
              AND scope = ?
              AND source_ref = ?
              AND cursor = ?
              AND message_redacted = ?
            ORDER BY stream_sequence DESC
            LIMIT 1
            """,
            (
                evidence.project_id,
                evidence.target_id,
                evidence.service_name,
                evidence.source_kind.value if evidence.source_kind is not None else None,
                evidence.scope.value if evidence.scope is not None else None,
                evidence.source_ref,
                evidence.cursor,
                evidence.content_redacted,
            ),
        ).fetchone()
    if row is None:
        return None
    return encode_log_cursor(int(row[0]))


def _evidence_snippets(
    evidence_refs: tuple[EvidenceRef, ...],
    *,
    facade_target_id: str,
    logs: LogStore,
) -> tuple[EvidenceSnippetView, ...]:
    return tuple(
        EvidenceSnippetView(
            evidence_ref_id=ref.evidence_ref_id,
            evidence_kind=ref.evidence_kind,
            target_id=facade_target_id,
            service_id=ref.service_name,
            source_kind=ref.source_kind,
            scope=ref.scope,
            severity=ref.severity,
            log_cursor=_log_cursor(logs, ref),
            summary=ref.content_redacted,
            created_at=ref.created_at,
        )
        for ref in evidence_refs
    )


class IssueProjectionService:
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
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._target_service = target_service
        self._target_store = target_store
        self._investigations = investigations
        self._approvals = approvals
        self._changes = changes
        self._evidence = evidence
        self._logs = logs
        self._now = now or (lambda: datetime.now(UTC))

    def list_issues(
        self,
        *,
        allowed_target_ids: frozenset[str] | None = None,
        target_id: str | None = None,
        service_id: str | None = None,
        status: IssueStatus | None = None,
        limit: int = 100,
        after: tuple[datetime, str] | None = None,
    ) -> IssuePage:
        if not (1 <= limit <= 500):
            raise ValueError("limit must be between 1 and 500")
        items = self._all_issues(
            allowed_target_ids=allowed_target_ids,
            target_id=target_id,
            service_id=service_id,
        )
        if status is not None:
            items = tuple(item for item in items if item.status is status)
        ordered = sorted(
            items,
            key=lambda item: (item.created_at, item.issue_id),
        )
        if after is not None:
            after_created_at = _utc(after[0])
            ordered = [
                item
                for item in ordered
                if item.created_at > after_created_at
                or (item.created_at == after_created_at and item.issue_id > after[1])
            ]
        has_more = len(ordered) > limit
        selected = tuple(ordered[:limit])
        next_cursor = (
            _encode_cursor(selected[-1].created_at, selected[-1].issue_id)
            if selected and has_more
            else None
        )
        return IssuePage(items=selected, next_cursor=next_cursor, has_more=has_more)

    def get_issue(
        self,
        issue_id: str,
        *,
        allowed_target_ids: frozenset[str] | None = None,
    ) -> IssueView | None:
        if not issue_id.startswith("iss_"):
            return None
        investigation_id = issue_id[4:]
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
        return self._build_issue(
            investigation,
            facade_target_id=facade_target_id,
        )

    def _all_issues(
        self,
        *,
        allowed_target_ids: frozenset[str] | None,
        target_id: str | None,
        service_id: str | None,
    ) -> tuple[IssueView, ...]:
        generated_at = _utc(self._now())
        targets = _target_lookup(self._target_service, self._target_store, now=generated_at)
        items: list[IssueView] = []
        for investigation in self._investigations.list_investigations():
            facade_target_id = targets.get((investigation.project_id, investigation.target_id))
            if facade_target_id is None:
                continue
            if allowed_target_ids is not None and facade_target_id not in allowed_target_ids:
                continue
            if target_id is not None and facade_target_id != target_id:
                continue
            if service_id is not None and investigation.service != service_id:
                continue
            items.append(
                self._build_issue(
                    investigation,
                    facade_target_id=facade_target_id,
                )
            )
        return tuple(items)

    def _build_issue(self, investigation, *, facade_target_id: str) -> IssueView:
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
        latest_change = change_sets[0] if change_sets else None
        validations = tuple(
            ref for ref in evidence_refs if ref.evidence_kind is EvidenceKind.VALIDATION_RESULT
        )
        latest_validation = validations[-1] if validations else None
        severity = max(
            (ref.severity for ref in evidence_refs if ref.severity is not None),
            key=_severity_rank,
            default=None,
        )
        pending_approval_ids = _matching_pending_approvals(
            self._approvals,
            investigation_id=investigation.investigation_id,
            facade_target_id=facade_target_id,
            registry_target_id=investigation.target_id,
            service_name=investigation.service,
        )
        return IssueView(
            issue_id=_issue_id(investigation.investigation_id),
            investigation_id=investigation.investigation_id,
            target_id=facade_target_id,
            service_id=investigation.service,
            symptom=investigation.symptom,
            status=_status_for(investigation.status, latest_change, latest_validation),
            severity=severity,
            root_cause=_root_cause(self._investigations, investigation.investigation_id),
            root_cause_confidence=None,
            pending_approval_ids=pending_approval_ids,
            evidence=_evidence_snippets(
                evidence_refs,
                facade_target_id=facade_target_id,
                logs=self._logs,
            ),
            resolution=(
                _changeset_summary(latest_change) if latest_change is not None else None
            ),
            verification=(
                _validation_summary(latest_validation)
                if latest_validation is not None
                else None
            ),
            created_at=investigation.created_at,
            updated_at=investigation.updated_at,
            started_at=investigation.started_at,
            completed_at=investigation.completed_at,
        )


__all__ = [
    "ChangeSummaryView",
    "EvidenceSnippetView",
    "IssuePage",
    "IssueProjectionService",
    "IssueStatus",
    "IssueView",
    "VerificationSummaryView",
    "_matching_changes",
    "_matching_evidence",
    "_issue_id",
    "_target_lookup",
    "decode_issue_cursor",
]
