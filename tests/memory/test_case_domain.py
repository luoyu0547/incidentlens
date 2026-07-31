"""Tests for case domain contracts — TDD RED phase."""

import pytest
from incidentlens_control_plane.memory.domain import (
    CaseDraft,
    CaseStatus,
    FeedbackRating,
)
from pydantic import ValidationError


def test_case_status_is_exactly_the_five_approved_values() -> None:
    assert {item.value for item in CaseStatus} == {
        "draft",
        "agent_generated",
        "human_verified",
        "deprecated",
        "rejected",
    }


def test_feedback_rating_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        CaseDraft(
            symptom="timeouts",
            affected_services=["order-service"],
            feedback="excellent",
        )


def test_case_draft_requires_symptom_and_service() -> None:
    with pytest.raises(ValidationError):
        CaseDraft(symptom="", affected_services=[])


def test_feedback_enum_is_exact() -> None:
    assert {item.value for item in FeedbackRating} == {
        "helpful", "partial", "irrelevant", "stale", "wrong"
    }
