"""CaseService — governs the case lifecycle via explicit state transitions.

Public interface:
  - create_draft(draft, actor) -> CaseSnapshot
  - materialize_from_investigation(state) -> CaseSnapshot
  - edit(case_id, expected_version, patch, actor, reason) -> CaseSnapshot
  - confirm(case_id, expected_version, actor, reason) -> CaseSnapshot
  - reject(case_id, expected_version, actor, reason) -> CaseSnapshot
  - deprecate(case_id, expected_version, actor, reason) -> CaseSnapshot
  - add_feedback(command) -> FeedbackRecord

Only ``human_verified`` cases enter the formal FTS search index.
Historical cases can only produce candidate hypotheses.
Final root cause must still pass Phase 4's current-incident Evidence gate.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from incidentlens_contracts.models import InvestigationStatus
from sqlalchemy.exc import IntegrityError

from incidentlens_control_plane.agent.state import InvestigationState
from incidentlens_control_plane.memory.domain import (
    CaseDraft,
    CaseSnapshot,
    CaseStatus,
    FeedbackCommand,
    FeedbackRating,
    FeedbackRecord,
    ReviewAction,
)
from incidentlens_control_plane.memory.models import (
    CaseFeedbackRow,
    CaseReviewActionRow,
    CaseRow,
)
from incidentlens_control_plane.memory.repository import CaseRepository

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CaseNotFoundError(Exception):
    """Raised when a case does not exist."""


class CaseConflictError(Exception):
    """Raised on optimistic-lock (revision) mismatch."""


class InvalidCaseTransitionError(Exception):
    """Raised when the requested state transition is not allowed."""


class CaseValidationError(Exception):
    """Raised when the case content is incomplete for the requested action."""


# ---------------------------------------------------------------------------
# Transition map
# ---------------------------------------------------------------------------

ALLOWED_TRANSITIONS: dict[tuple[CaseStatus, ReviewAction], CaseStatus] = {
    (CaseStatus.DRAFT, ReviewAction.EDIT): CaseStatus.DRAFT,
    (CaseStatus.AGENT_GENERATED, ReviewAction.EDIT): CaseStatus.DRAFT,
    (CaseStatus.HUMAN_VERIFIED, ReviewAction.EDIT): CaseStatus.DRAFT,
    (CaseStatus.REJECTED, ReviewAction.EDIT): CaseStatus.DRAFT,
    (CaseStatus.DEPRECATED, ReviewAction.EDIT): CaseStatus.DRAFT,
    (CaseStatus.DRAFT, ReviewAction.CONFIRM): CaseStatus.HUMAN_VERIFIED,
    (CaseStatus.AGENT_GENERATED, ReviewAction.CONFIRM): CaseStatus.HUMAN_VERIFIED,
    (CaseStatus.DRAFT, ReviewAction.REJECT): CaseStatus.REJECTED,
    (CaseStatus.AGENT_GENERATED, ReviewAction.REJECT): CaseStatus.REJECTED,
    (CaseStatus.HUMAN_VERIFIED, ReviewAction.DEPRECATE): CaseStatus.DEPRECATED,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_revision(row: CaseRow, expected_version: int) -> None:
    """Optimistic-lock check."""
    if row.revision != expected_version:
        raise CaseConflictError(
            f"case {row.id} revision is {row.revision}, expected {expected_version}"
        )


def _row_to_snapshot(row: CaseRow) -> CaseSnapshot:
    """Convert an ORM row to an immutable read model."""
    affected = json.loads(row.affected_services_json) if row.affected_services_json else []
    evidence = json.loads(row.key_evidence_json) if row.key_evidence_json else []
    path = json.loads(row.investigation_path_json) if row.investigation_path_json else []
    invalid = json.loads(row.invalid_hypotheses_json) if row.invalid_hypotheses_json else []
    advice = json.loads(row.remediation_advice_json) if row.remediation_advice_json else []
    applic = (
        json.loads(row.applicability_conditions_json) if row.applicability_conditions_json else []
    )
    inapplic = (
        json.loads(row.inapplicability_conditions_json)
        if row.inapplicability_conditions_json
        else []
    )
    return CaseSnapshot(
        id=row.id,
        revision=row.revision,
        status=CaseStatus(row.status),
        incident_id=row.incident_id,
        source_reference=row.source_reference,
        symptom=row.symptom,
        affected_services=affected,
        root_cause_category=row.root_cause_category,
        root_cause_description=row.root_cause_description,
        key_evidence=evidence,
        investigation_path=path,
        invalid_hypotheses=invalid,
        resolution=row.resolution,
        remediation_advice=advice,
        applicability_conditions=applic,
        inapplicability_conditions=inapplic,
        environment=row.environment,
        service_version_exact=row.service_version_exact,
        service_version_min=row.service_version_min,
        service_version_max=row.service_version_max,
        source_report_json=row.source_report_json,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _draft_to_row(
    draft: CaseDraft, *, status: str = "draft", incident_id: str | None = None
) -> CaseRow:
    """Create a CaseRow from a CaseDraft."""
    now = datetime.now(tz=timezone.utc)
    return CaseRow(
        incident_id=incident_id,
        status=status,
        revision=1,
        symptom=draft.symptom,
        affected_services_json=json.dumps(draft.affected_services),
        root_cause_category=draft.root_cause_category,
        root_cause_description=draft.root_cause_description,
        key_evidence_json=json.dumps(draft.key_evidence),
        investigation_path_json=json.dumps(draft.investigation_path),
        invalid_hypotheses_json=json.dumps(draft.invalid_hypotheses),
        resolution=draft.resolution,
        remediation_advice_json=json.dumps(draft.remediation_advice),
        applicability_conditions_json=json.dumps(draft.applicability_conditions),
        inapplicability_conditions_json=json.dumps(draft.inapplicability_conditions),
        environment=draft.environment,
        service_version_exact=draft.service_version_exact,
        service_version_min=draft.service_version_min,
        service_version_max=draft.service_version_max,
        source_report_json="{}",
        created_at=now,
        updated_at=now,
    )


def _apply_patch_to_row(row: CaseRow, patch: CaseDraft) -> None:
    """Mutate *row* in-place with the fields from *patch*."""
    row.symptom = patch.symptom
    row.affected_services_json = json.dumps(patch.affected_services)
    row.root_cause_category = patch.root_cause_category
    row.root_cause_description = patch.root_cause_description
    row.key_evidence_json = json.dumps(patch.key_evidence)
    row.investigation_path_json = json.dumps(patch.investigation_path)
    row.invalid_hypotheses_json = json.dumps(patch.invalid_hypotheses)
    row.resolution = patch.resolution
    row.remediation_advice_json = json.dumps(patch.remediation_advice)
    row.applicability_conditions_json = json.dumps(patch.applicability_conditions)
    row.inapplicability_conditions_json = json.dumps(patch.inapplicability_conditions)
    row.environment = patch.environment
    row.service_version_exact = patch.service_version_exact
    row.service_version_min = patch.service_version_min
    row.service_version_max = patch.service_version_max
    row.updated_at = datetime.now(tz=timezone.utc)


def _validate_for_confirm(row: CaseRow) -> None:
    """Ensure the case has sufficient content for confirmation."""
    errors: list[str] = []
    if not row.root_cause_category:
        errors.append("root_cause_category")
    if not row.root_cause_description:
        errors.append("root_cause_description")
    key_evidence = json.loads(row.key_evidence_json) if row.key_evidence_json else []
    if not key_evidence:
        errors.append("key_evidence")
    resolution = row.resolution or ""
    remediation = json.loads(row.remediation_advice_json) if row.remediation_advice_json else []
    if not resolution and not remediation:
        errors.append("resolution or remediation_advice")
    if errors:
        raise CaseValidationError(
            f"Cannot confirm case: missing required fields: {', '.join(errors)}"
        )


# ---------------------------------------------------------------------------
# CaseService
# ---------------------------------------------------------------------------


class CaseService:
    """Governs the case lifecycle through explicit state transitions."""

    def __init__(self, repository: CaseRepository) -> None:
        self.repo = repository

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_draft(self, draft: CaseDraft, actor: str) -> CaseSnapshot:
        """Create a new draft case.

        The ``POST /api/cases`` endpoint must always produce a ``draft``.
        Clients cannot write a target status directly.
        """
        with self.repo.transaction() as session:
            row = _draft_to_row(draft, status="draft")
            row = self.repo.add_case(session, row)
            review = CaseReviewActionRow(
                case_id=row.id,
                action=ReviewAction.CREATE,
                actor=actor,
                reason="",
                previous_status=None,
                new_status=CaseStatus.DRAFT,
            )
            self.repo.add_review(session, review)
            return _row_to_snapshot(row)

    def materialize_from_investigation(
        self,
        state: InvestigationState,
    ) -> CaseSnapshot:
        """Create or return an existing ``agent_generated`` case.

        Rejects states that are not ``report_ready``.  Idempotent by
        ``incident_id``: if a row with the same ``incident_id`` exists,
        return it without adding a second review record.
        """
        if state.status != InvestigationStatus.REPORT_READY:
            raise CaseValidationError(
                f"Cannot materialize from non-report_ready state: {state.status}"
            )

        draft = CaseDraft(
            symptom=str(state.alert.get("symptom") or state.alert),
            affected_services=list(
                dict.fromkeys(
                    [
                        s
                        for s in [
                            str(state.alert.get("service", "")),
                            str(state.report.get("root_service", "")) if state.report else "",
                        ]
                        if s
                    ]
                )
            )
            or [str(state.alert.get("service", ""))],
            root_cause_category=str(state.report.get("root_cause", "")) if state.report else "",
            root_cause_description=str(state.report.get("root_cause", "")) if state.report else "",
            key_evidence=list(state.report.get("findings", [])) if state.report else [],
            investigation_path=[{"round": state.current_round, "phase": state.phase}],
            invalid_hypotheses=[
                h.model_dump(mode="json") for h in state.hypotheses if str(h.status) == "ruled_out"
            ],
            remediation_advice=[],
        )

        with self.repo.transaction() as session:
            existing = self.repo.get_by_incident(session, state.incident_id)
            if existing is not None:
                return _row_to_snapshot(existing)

            row = _draft_to_row(draft, status="agent_generated", incident_id=state.incident_id)
            row.source_report_json = json.dumps(state.report, default=str) if state.report else "{}"
            row = self.repo.add_case(session, row)

            review = CaseReviewActionRow(
                case_id=row.id,
                action=ReviewAction.MATERIALIZE,
                actor="system",
                reason=f"materialized from investigation {state.incident_id}",
                previous_status=None,
                new_status=CaseStatus.AGENT_GENERATED,
            )
            self.repo.add_review(session, review)
            return _row_to_snapshot(row)

    def edit(
        self,
        case_id: int,
        expected_version: int,
        patch: CaseDraft,
        actor: str,
        reason: str = "",
    ) -> CaseSnapshot:
        """Edit a case and move it back to ``draft``.

        Any status can be edited; the case always returns to ``draft``.
        Modifying a verified case immediately removes it from formal search.
        """
        action = ReviewAction.EDIT
        with self.repo.transaction() as session:
            row = self.repo.get_case(session, case_id)
            if row is None:
                raise CaseNotFoundError(f"case {case_id} not found")
            _assert_revision(row, expected_version)

            current_status = CaseStatus(row.status)
            target = ALLOWED_TRANSITIONS.get((current_status, action))
            if target is None:
                raise InvalidCaseTransitionError(
                    f"Cannot {action.value} from {current_status.value}"
                )

            previous_status = row.status
            _apply_patch_to_row(row, patch)
            row.status = target.value
            row.revision += 1
            row.updated_at = datetime.now(tz=timezone.utc)

            review = CaseReviewActionRow(
                case_id=case_id,
                action=action,
                actor=actor,
                reason=reason,
                previous_status=previous_status,
                new_status=target.value,
            )
            self.repo.add_review(session, review)

            # Editing always removes from FTS (only verified cases are indexed,
            # and moving to draft means it should be removed).
            self.repo.remove_fts(session, case_id)

            return _row_to_snapshot(row)

    def confirm(
        self,
        case_id: int,
        expected_version: int,
        actor: str,
        reason: str = "",
    ) -> CaseSnapshot:
        """Confirm a draft or agent_generated case as ``human_verified``.

        Requires non-empty root_cause_category, root_cause_description,
        key_evidence, and at least one of resolution or remediation_advice.
        """
        action = ReviewAction.CONFIRM
        with self.repo.transaction() as session:
            row = self.repo.get_case(session, case_id)
            if row is None:
                raise CaseNotFoundError(f"case {case_id} not found")
            _assert_revision(row, expected_version)

            current_status = CaseStatus(row.status)
            target = ALLOWED_TRANSITIONS.get((current_status, action))
            if target is None:
                raise InvalidCaseTransitionError(
                    f"Cannot {action.value} from {current_status.value}"
                )

            # Validate review content completeness
            _validate_for_confirm(row)

            previous_status = row.status
            row.status = target.value
            row.revision += 1
            row.updated_at = datetime.now(tz=timezone.utc)

            review = CaseReviewActionRow(
                case_id=case_id,
                action=action,
                actor=actor,
                reason=reason,
                previous_status=previous_status,
                new_status=target.value,
            )
            self.repo.add_review(session, review)

            # Add to FTS index
            self.repo.replace_fts(session, row)

            return _row_to_snapshot(row)

    def reject(
        self,
        case_id: int,
        expected_version: int,
        actor: str,
        reason: str,
    ) -> CaseSnapshot:
        """Reject a draft or agent_generated case."""
        action = ReviewAction.REJECT
        with self.repo.transaction() as session:
            row = self.repo.get_case(session, case_id)
            if row is None:
                raise CaseNotFoundError(f"case {case_id} not found")
            _assert_revision(row, expected_version)

            current_status = CaseStatus(row.status)
            target = ALLOWED_TRANSITIONS.get((current_status, action))
            if target is None:
                raise InvalidCaseTransitionError(
                    f"Cannot {action.value} from {current_status.value}"
                )

            previous_status = row.status
            row.status = target.value
            row.revision += 1
            row.updated_at = datetime.now(tz=timezone.utc)

            review = CaseReviewActionRow(
                case_id=case_id,
                action=action,
                actor=actor,
                reason=reason,
                previous_status=previous_status,
                new_status=target.value,
            )
            self.repo.add_review(session, review)

            # Rejected cases are not in FTS
            self.repo.remove_fts(session, case_id)

            return _row_to_snapshot(row)

    def deprecate(
        self,
        case_id: int,
        expected_version: int,
        actor: str,
        reason: str,
    ) -> CaseSnapshot:
        """Deprecate a human_verified case."""
        action = ReviewAction.DEPRECATE
        with self.repo.transaction() as session:
            row = self.repo.get_case(session, case_id)
            if row is None:
                raise CaseNotFoundError(f"case {case_id} not found")
            _assert_revision(row, expected_version)

            current_status = CaseStatus(row.status)
            target = ALLOWED_TRANSITIONS.get((current_status, action))
            if target is None:
                raise InvalidCaseTransitionError(
                    f"Cannot {action.value} from {current_status.value}"
                )

            previous_status = row.status
            row.status = target.value
            row.revision += 1
            row.updated_at = datetime.now(tz=timezone.utc)

            review = CaseReviewActionRow(
                case_id=case_id,
                action=action,
                actor=actor,
                reason=reason,
                previous_status=previous_status,
                new_status=target.value,
            )
            self.repo.add_review(session, review)

            # Deprecated cases are removed from FTS
            self.repo.remove_fts(session, case_id)

            return _row_to_snapshot(row)

    def get_by_incident(self, incident_id: str) -> dict[str, Any] | None:
        """Return the case snapshot (as a dict) linked to *incident_id*, or None.

        Used by the export service to include the associated case in the
        export payload.
        """
        with self.repo.transaction() as session:
            row = self.repo.get_by_incident(session, incident_id)
            if row is None:
                return None
            return _row_to_snapshot(row).model_dump(mode="json")

    def list_usage(self, incident_id: str) -> list:
        """Return all usage events for *incident_id*.

        Used by the export service to include usage events in the export
        payload. Returns a list of ``CaseUsageEvent`` domain objects.
        """
        from incidentlens_control_plane.memory.domain import (
            CaseUsageEvent,
            UsageEventType,
        )
        from incidentlens_control_plane.memory.models import CaseUsageEventRow

        with self.repo.transaction() as session:
            rows = (
                session.query(CaseUsageEventRow)
                .filter(CaseUsageEventRow.investigation_id == incident_id)
                .order_by(CaseUsageEventRow.id)
                .all()
            )
            return [
                CaseUsageEvent(
                    id=row.id,
                    case_id=row.case_id,
                    incident_id=row.investigation_id or "",
                    hypothesis_id=row.hypothesis_id,
                    event_type=UsageEventType(row.event_type),
                    idempotency_key=row.idempotency_key,
                    details=json.loads(row.details_json) if row.details_json else {},
                )
                for row in rows
            ]

    def add_feedback(self, command: FeedbackCommand) -> FeedbackRecord:
        """Record feedback on a case search result.

        Idempotent by ``idempotency_key``: re-sending the same key
        returns the original feedback without duplicating.
        """
        with self.repo.transaction() as session:
            row = self.repo.get_case(session, command.case_id)
            if row is None:
                raise CaseNotFoundError(f"case {command.case_id} not found")

            # Check for existing feedback with the same idempotency key
            existing = (
                session.query(CaseFeedbackRow)
                .filter(CaseFeedbackRow.idempotency_key == command.idempotency_key)
                .first()
            )
            if existing is not None:
                return FeedbackRecord(
                    id=existing.id,
                    case_id=existing.case_id,
                    idempotency_key=existing.idempotency_key,
                    rating=FeedbackRating(existing.rating),
                    incident_id=existing.incident_id,
                    actor=existing.actor,
                    comment=existing.comment,
                    created_at=existing.created_at,
                )

            feedback_row = CaseFeedbackRow(
                case_id=command.case_id,
                idempotency_key=command.idempotency_key,
                rating=command.rating.value,
                incident_id=command.incident_id,
                actor=command.actor,
                comment=command.comment,
            )
            try:
                self.repo.add_feedback(session, feedback_row)
                session.flush()
            except IntegrityError:
                # Race condition: another transaction inserted the same key.
                # Roll back and return the existing record.
                session.rollback()
                with self.repo.transaction() as retry_session:
                    existing = (
                        retry_session.query(CaseFeedbackRow)
                        .filter(CaseFeedbackRow.idempotency_key == command.idempotency_key)
                        .first()
                    )
                    if existing is None:
                        raise  # pragma: no cover
                    return FeedbackRecord(
                        id=existing.id,
                        case_id=existing.case_id,
                        idempotency_key=existing.idempotency_key,
                        rating=FeedbackRating(existing.rating),
                        incident_id=existing.incident_id,
                        actor=existing.actor,
                        comment=existing.comment,
                        created_at=existing.created_at,
                    )

            return FeedbackRecord(
                id=feedback_row.id,
                case_id=feedback_row.case_id,
                idempotency_key=feedback_row.idempotency_key,
                rating=FeedbackRating(feedback_row.rating),
                incident_id=feedback_row.incident_id,
                actor=feedback_row.actor,
                comment=feedback_row.comment,
                created_at=feedback_row.created_at,
            )
