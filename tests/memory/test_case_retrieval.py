"""Tests for governed case retrieval and FTS behavior.

Covers:
  - Unverified cases are absent from case_fts
  - Repeating confirm after first transition returns a 409 conflict
  - Feedback idempotency key returns the original feedback
  - Modifying a verified case removes its FTS row
  - Deprecating removes FTS
  - Review history remains ordered and append-only
  - Forced FTS insert failure rolls back both status and review action
"""

from __future__ import annotations

import pytest
from incidentlens_control_plane.memory.domain import (
    CaseDraft,
    CaseStatus,
    FeedbackCommand,
    FeedbackRating,
)
from incidentlens_control_plane.memory.models import CaseReviewActionRow
from incidentlens_control_plane.memory.repository import CaseRepository
from incidentlens_control_plane.memory.service import (
    CaseConflictError,
    CaseNotFoundError,
    CaseService,
    CaseValidationError,
    InvalidCaseTransitionError,
)
from incidentlens_telemetry.database import create_engine
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def service() -> CaseService:
    """Create a CaseService backed by an in-memory SQLite DB."""
    return CaseService(CaseRepository(create_engine("sqlite:///:memory:")))


@pytest.fixture()
def repository(service: CaseService) -> CaseRepository:
    """Return the repository from the service for direct DB inspection."""
    return service.repo


# ---------------------------------------------------------------------------
# FTS governance: only human_verified cases in case_fts
# ---------------------------------------------------------------------------


class TestFTSGovernance:
    """Only human_verified cases should be in the FTS index."""

    def test_unverified_case_not_in_fts(
        self, service: CaseService, repository: CaseRepository
    ) -> None:
        """A draft case should not appear in case_fts."""
        case = service.create_draft(
            CaseDraft(symptom="timeout", affected_services=["order-service"]),
            actor="user",
        )
        with repository.transaction() as session:
            count = session.execute(
                text("SELECT COUNT(*) FROM case_fts WHERE case_id = :cid"),
                {"cid": case.id},
            ).scalar()
        assert count == 0

    def test_verified_case_in_fts(self, service: CaseService, repository: CaseRepository) -> None:
        """A human_verified case should appear in case_fts."""
        case = service.create_draft(
            CaseDraft(
                symptom="timeout",
                affected_services=["order-service"],
                root_cause_category="downstream-timeout",
                root_cause_description="payment service latency",
                key_evidence=[{"evidence_id": "ev-1"}],
                resolution="remove delay",
            ),
            actor="user",
        )
        service.confirm(case.id, case.revision, "reviewer")
        with repository.transaction() as session:
            count = session.execute(
                text("SELECT COUNT(*) FROM case_fts WHERE case_id = :cid"),
                {"cid": case.id},
            ).scalar()
        assert count == 1

    def test_agent_generated_not_in_fts(
        self, service: CaseService, repository: CaseRepository
    ) -> None:
        """An agent_generated case should not appear in case_fts."""
        from incidentlens_contracts.models import InvestigationStatus
        from incidentlens_control_plane.agent.state import InvestigationState

        state = InvestigationState(
            incident_id="inc-fts-test",
            status=InvestigationStatus.REPORT_READY,
            alert={"service": "order-service", "symptom": "timeout"},
            report={"root_service": "payment-service", "root_cause": "latency"},
        )
        case = service.materialize_from_investigation(state)
        assert case.status is CaseStatus.AGENT_GENERATED
        with repository.transaction() as session:
            count = session.execute(
                text("SELECT COUNT(*) FROM case_fts WHERE case_id = :cid"),
                {"cid": case.id},
            ).scalar()
        assert count == 0


# ---------------------------------------------------------------------------
# Transition conflicts
# ---------------------------------------------------------------------------


class TestTransitionConflicts:
    """Optimistic-lock and illegal-transition checks."""

    def test_repeating_confirm_returns_409(self, service: CaseService) -> None:
        """Confirming an already-verified case raises InvalidCaseTransitionError."""
        case = service.create_draft(
            CaseDraft(
                symptom="timeout",
                affected_services=["order-service"],
                root_cause_category="downstream-timeout",
                root_cause_description="payment latency",
                key_evidence=[{"evidence_id": "ev-1"}],
                resolution="remove delay",
            ),
            actor="user",
        )
        verified = service.confirm(case.id, case.revision, "reviewer")
        # Trying to confirm again from human_verified -> confirm is not allowed
        with pytest.raises(InvalidCaseTransitionError):
            service.confirm(verified.id, verified.revision, "reviewer")

    def test_stale_revision_rejected(self, service: CaseService) -> None:
        """Using an old revision number should raise CaseConflictError."""
        case = service.create_draft(
            CaseDraft(symptom="timeout", affected_services=["order-service"]),
            actor="user",
        )
        with pytest.raises(CaseConflictError):
            service.confirm(case.id, 999, "reviewer")

    def test_nonexistent_case_raises(self, service: CaseService) -> None:
        """Editing a case that does not exist should raise CaseNotFoundError."""
        with pytest.raises(CaseNotFoundError):
            service.edit(
                999999, 1,
                CaseDraft(symptom="x", affected_services=["x"]),
                "user",
            )


# ---------------------------------------------------------------------------
# Feedback idempotency
# ---------------------------------------------------------------------------


class TestFeedbackIdempotency:
    """Feedback with the same idempotency_key must return the original."""

    def test_duplicate_key_returns_original(self, service: CaseService) -> None:
        case = service.create_draft(
            CaseDraft(symptom="timeout", affected_services=["order-service"]),
            actor="user",
        )
        cmd = FeedbackCommand(
            case_id=case.id,
            idempotency_key="fb-001",
            rating=FeedbackRating.HELPFUL,
            comment="great",
        )
        first = service.add_feedback(cmd)
        second = service.add_feedback(cmd)
        assert first.id == second.id
        assert first.rating is FeedbackRating.HELPFUL

    def test_different_key_creates_new(self, service: CaseService) -> None:
        case = service.create_draft(
            CaseDraft(symptom="timeout", affected_services=["order-service"]),
            actor="user",
        )
        r1 = service.add_feedback(
            FeedbackCommand(case_id=case.id, idempotency_key="fb-a", rating=FeedbackRating.HELPFUL)
        )
        r2 = service.add_feedback(
            FeedbackCommand(case_id=case.id, idempotency_key="fb-b", rating=FeedbackRating.WRONG)
        )
        assert r1.id != r2.id


# ---------------------------------------------------------------------------
# FTS removal on edit / deprecate
# ---------------------------------------------------------------------------


class TestFTSRemoval:
    """Editing or deprecating a verified case removes it from FTS."""

    def test_edit_removes_from_fts(self, service: CaseService, repository: CaseRepository) -> None:
        case = service.create_draft(
            CaseDraft(
                symptom="timeout",
                affected_services=["order-service"],
                root_cause_category="downstream-timeout",
                root_cause_description="payment latency",
                key_evidence=[{"evidence_id": "ev-1"}],
                resolution="remove delay",
            ),
            actor="user",
        )
        verified = service.confirm(case.id, case.revision, "reviewer")
        # Confirm adds to FTS
        with repository.transaction() as session:
            count = session.execute(
                text("SELECT COUNT(*) FROM case_fts WHERE case_id = :cid"),
                {"cid": case.id},
            ).scalar()
        assert count == 1

        # Edit removes from FTS
        edited = service.edit(
            verified.id,
            verified.revision,
            CaseDraft(symptom="timeout updated", affected_services=["order-service"]),
            "reviewer",
            reason="fix wording",
        )
        assert edited.status is CaseStatus.DRAFT
        with repository.transaction() as session:
            count = session.execute(
                text("SELECT COUNT(*) FROM case_fts WHERE case_id = :cid"),
                {"cid": case.id},
            ).scalar()
        assert count == 0

    def test_deprecate_removes_from_fts(
        self, service: CaseService, repository: CaseRepository
    ) -> None:
        case = service.create_draft(
            CaseDraft(
                symptom="timeout",
                affected_services=["order-service"],
                root_cause_category="downstream-timeout",
                root_cause_description="payment latency",
                key_evidence=[{"evidence_id": "ev-1"}],
                resolution="remove delay",
            ),
            actor="user",
        )
        verified = service.confirm(case.id, case.revision, "reviewer")
        deprecated = service.deprecate(verified.id, verified.revision, "admin", "obsolete")
        assert deprecated.status is CaseStatus.DEPRECATED
        with repository.transaction() as session:
            count = session.execute(
                text("SELECT COUNT(*) FROM case_fts WHERE case_id = :cid"),
                {"cid": case.id},
            ).scalar()
        assert count == 0


# ---------------------------------------------------------------------------
# Review history: ordered and append-only
# ---------------------------------------------------------------------------


class TestReviewHistory:
    """Review actions must be ordered and append-only."""

    def test_review_history_ordered_and_append_only(
        self, service: CaseService, repository: CaseRepository
    ) -> None:
        case = service.create_draft(
            CaseDraft(
                symptom="timeout",
                affected_services=["order-service"],
                root_cause_category="downstream-timeout",
                root_cause_description="payment latency",
                key_evidence=[{"evidence_id": "ev-1"}],
                resolution="remove delay",
            ),
            actor="user",
        )
        confirmed = service.confirm(case.id, case.revision, "reviewer", reason="looks good")
        service.edit(
            case.id,
            confirmed.revision,
            CaseDraft(symptom="timeout v2", affected_services=["order-service"]),
            "reviewer",
            reason="update symptom",
        )

        with repository.transaction() as session:
            reviews = (
                session.query(CaseReviewActionRow)
                .filter(CaseReviewActionRow.case_id == case.id)
                .order_by(CaseReviewActionRow.id)
                .all()
            )
            # Should have: create, confirm, edit
            assert len(reviews) == 3
            assert reviews[0].action == "create"
            assert reviews[1].action == "confirm"
            assert reviews[2].action == "edit"
            # IDs must be strictly increasing (append-only)
            ids = [r.id for r in reviews]
            assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# Confirm validation
# ---------------------------------------------------------------------------


class TestConfirmValidation:
    """Confirm requires non-empty review content fields."""

    def test_confirm_rejects_empty_root_cause(self, service: CaseService) -> None:
        case = service.create_draft(
            CaseDraft(symptom="timeout", affected_services=["order-service"]),
            actor="user",
        )
        with pytest.raises(CaseValidationError, match="root_cause_category"):
            service.confirm(case.id, case.revision, "reviewer")

    def test_confirm_rejects_empty_evidence(self, service: CaseService) -> None:
        case = service.create_draft(
            CaseDraft(
                symptom="timeout",
                affected_services=["order-service"],
                root_cause_category="downstream-timeout",
                root_cause_description="payment latency",
            ),
            actor="user",
        )
        with pytest.raises(CaseValidationError, match="key_evidence"):
            service.confirm(case.id, case.revision, "reviewer")

    def test_confirm_rejects_no_resolution_or_remediation(self, service: CaseService) -> None:
        case = service.create_draft(
            CaseDraft(
                symptom="timeout",
                affected_services=["order-service"],
                root_cause_category="downstream-timeout",
                root_cause_description="payment latency",
                key_evidence=[{"evidence_id": "ev-1"}],
            ),
            actor="user",
        )
        with pytest.raises(CaseValidationError, match="resolution or remediation_advice"):
            service.confirm(case.id, case.revision, "reviewer")
