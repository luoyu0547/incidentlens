"""Case memory domain contracts.

Defines the exact domain enums, validated commands, and read models
for the governed case memory schema (Phase 5).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CaseStatus(StrEnum):
    """Lifecycle states for a case."""

    DRAFT = "draft"
    AGENT_GENERATED = "agent_generated"
    HUMAN_VERIFIED = "human_verified"
    DEPRECATED = "deprecated"
    REJECTED = "rejected"


class ReviewAction(StrEnum):
    """Append-only audit actions recorded in case_review_actions."""

    CREATE = "create"
    MATERIALIZE = "materialize"
    EDIT = "edit"
    CONFIRM = "confirm"
    REJECT = "reject"
    DEPRECATE = "deprecate"


class FeedbackRating(StrEnum):
    """Ratings for case search feedback."""

    HELPFUL = "helpful"
    PARTIAL = "partial"
    IRRELEVANT = "irrelevant"
    STALE = "stale"
    WRONG = "wrong"


class UsageEventType(StrEnum):
    """Tracks how a case was used in an investigation."""

    RECALLED = "recalled"
    ADOPTED = "adopted"
    VALIDATED = "validated"
    MISLEADING = "misleading"


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


class CaseSnapshot(BaseModel):
    """Immutable read model returned by the service layer."""

    model_config = ConfigDict(extra="forbid")

    id: int
    revision: int
    status: CaseStatus
    incident_id: str | None = None
    source_reference: str = ""
    symptom: str
    affected_services: list[str]
    root_cause_category: str = ""
    root_cause_description: str = ""
    key_evidence: list[dict[str, Any]] = Field(default_factory=list)
    investigation_path: list[dict[str, Any]] = Field(default_factory=list)
    invalid_hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    resolution: str = ""
    remediation_advice: list[str] = Field(default_factory=list)
    applicability_conditions: list[str] = Field(default_factory=list)
    inapplicability_conditions: list[str] = Field(default_factory=list)
    environment: str = ""
    service_version_exact: str = ""
    service_version_min: str = ""
    service_version_max: str = ""
    source_report_json: str = ""
    created_at: datetime
    updated_at: datetime


class CaseSearchQuery(BaseModel):
    """Parameters for FTS5 search."""

    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=1, max_length=500)
    service: str | None = None
    root_cause_category: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class CaseSearchHit(BaseModel):
    """A single search result from FTS5."""

    model_config = ConfigDict(extra="forbid")

    case_id: int
    revision: int
    status: CaseStatus
    symptom: str
    affected_services: list[str]
    root_cause_category: str = ""
    rank: float = 0.0


# ---------------------------------------------------------------------------
# Write commands (no status field — clients cannot set target status)
# ---------------------------------------------------------------------------


class CaseDraft(BaseModel):
    """Validated input for creating or editing a case."""

    model_config = ConfigDict(extra="forbid")

    symptom: str = Field(min_length=1, max_length=4000)
    affected_services: list[str] = Field(min_length=1, max_length=20)
    root_cause_category: str = Field(default="", max_length=255)
    root_cause_description: str = Field(default="", max_length=8000)
    key_evidence: list[dict[str, Any]] = Field(default_factory=list)
    investigation_path: list[dict[str, Any]] = Field(default_factory=list)
    invalid_hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    resolution: str = Field(default="", max_length=8000)
    remediation_advice: list[str] = Field(default_factory=list)
    applicability_conditions: list[str] = Field(default_factory=list)
    inapplicability_conditions: list[str] = Field(default_factory=list)
    environment: str = Field(default="", max_length=255)
    service_version_exact: str = Field(default="", max_length=255)
    service_version_min: str = Field(default="", max_length=255)
    service_version_max: str = Field(default="", max_length=255)

    @model_validator(mode="after")
    def validate_versions(self) -> CaseDraft:
        if self.service_version_exact and (
            self.service_version_min or self.service_version_max
        ):
            raise ValueError("exact version cannot be combined with a version range")
        return self


class FeedbackCommand(BaseModel):
    """Command to record feedback on a case search result."""

    model_config = ConfigDict(extra="forbid")

    case_id: int
    idempotency_key: str = Field(min_length=1, max_length=255)
    rating: FeedbackRating
    comment: str = Field(default="", max_length=4000)


class ReviewCommand(BaseModel):
    """Command to perform a review action on a case."""

    model_config = ConfigDict(extra="forbid")

    case_id: int
    action: ReviewAction
    actor: str = Field(min_length=1, max_length=255)
    reason: str = Field(default="", max_length=4000)
