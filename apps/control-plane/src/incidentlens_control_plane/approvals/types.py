"""Approval domain types."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"


class ApprovalDecisionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalDownstreamStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str = Field(min_length=1, max_length=120)
    intent_sha256: str = Field(min_length=1, max_length=64)
    intent: dict[str, object]
    intent_summary: str = Field(min_length=1, max_length=1000)
    status: ApprovalStatus
    project_id: str | None = Field(default=None, min_length=1, max_length=80)
    target_id: str | None = Field(default=None, min_length=1, max_length=120)
    service: str | None = Field(default=None, min_length=1, max_length=120)
    session_id: str | None = Field(default=None, min_length=1, max_length=120)
    investigation_id: str | None = Field(default=None, min_length=1, max_length=120)
    agent_run_id: str | None = Field(default=None, min_length=1, max_length=120)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=120)
    changeset_id: str | None = Field(default=None, min_length=1, max_length=120)
    proposal_id: str | None = Field(default=None, min_length=1, max_length=120)
    risk: str = Field(default="approval_required", min_length=1, max_length=80)
    preview: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    consumed_at: datetime | None = None
    decision_actor: str | None = Field(default=None, min_length=1, max_length=200)
    decision_reason: str | None = Field(default=None, min_length=1, max_length=1000)
    decision_route_key: str | None = Field(default=None, min_length=1, max_length=200)
    decision_idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    decision_request_sha256: str | None = Field(default=None, min_length=1, max_length=64)
    downstream_status: ApprovalDownstreamStatus = ApprovalDownstreamStatus.NOT_APPLICABLE
    downstream_error_code: str | None = Field(default=None, min_length=1, max_length=120)
    downstream_updated_at: datetime | None = None

    @property
    def decision_status(self) -> ApprovalDecisionStatus:
        if self.status in (ApprovalStatus.APPROVED, ApprovalStatus.CONSUMED):
            return ApprovalDecisionStatus.APPROVED
        if self.status is ApprovalStatus.REJECTED:
            return ApprovalDecisionStatus.REJECTED
        return ApprovalDecisionStatus.PENDING

    @property
    def has_downstream_linkage(self) -> bool:
        return any(
            value
            for value in (
                self.session_id,
                self.investigation_id,
                self.agent_run_id,
                self.tool_call_id,
                self.changeset_id,
                self.proposal_id,
            )
        )
