"""Immutable change value types and ChangeSet state machine."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ChangeSetStatus(StrEnum):
    DRAFT = "draft"
    PREFLIGHTED = "preflighted"
    LOCALLY_BACKED_UP = "locally_backed_up"
    REMOTELY_BACKED_UP = "remotely_backed_up"
    APPLIED = "applied"
    VALIDATED = "validated"
    VERIFIED = "verified"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


# Allowed transitions: target -> set of valid predecessors
_VALID_PREDECESSORS: dict[ChangeSetStatus, set[ChangeSetStatus]] = {
    ChangeSetStatus.PREFLIGHTED: {ChangeSetStatus.DRAFT},
    ChangeSetStatus.LOCALLY_BACKED_UP: {ChangeSetStatus.PREFLIGHTED},
    ChangeSetStatus.REMOTELY_BACKED_UP: {ChangeSetStatus.LOCALLY_BACKED_UP},
    ChangeSetStatus.APPLIED: {ChangeSetStatus.REMOTELY_BACKED_UP},
    ChangeSetStatus.VALIDATED: {ChangeSetStatus.APPLIED},
    ChangeSetStatus.VERIFIED: {ChangeSetStatus.VALIDATED},
    # failed is reachable from any active (non-terminal) state
    ChangeSetStatus.FAILED: {
        ChangeSetStatus.DRAFT,
        ChangeSetStatus.PREFLIGHTED,
        ChangeSetStatus.LOCALLY_BACKED_UP,
        ChangeSetStatus.REMOTELY_BACKED_UP,
        ChangeSetStatus.APPLIED,
        ChangeSetStatus.VALIDATED,
    },
    # rolled_back requires at least one file was applied
    ChangeSetStatus.ROLLED_BACK: {ChangeSetStatus.APPLIED, ChangeSetStatus.VALIDATED},
}

# Terminal states that accept no further transitions
_TERMINAL_STATES: frozenset[ChangeSetStatus] = frozenset(
    {
        ChangeSetStatus.VERIFIED,
        ChangeSetStatus.FAILED,
        ChangeSetStatus.ROLLED_BACK,
    }
)


class FileChange(BaseModel):
    """Immutable record describing a single file-level change within a ChangeSet."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_change_id: str = Field(min_length=1, max_length=120)
    scope: str = Field(min_length=1, max_length=80)
    remote_path: str = Field(min_length=1, max_length=512)
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    replacement_sha256: str = Field(min_length=64, max_length=64)
    diff_text: str = ""
    original_metadata: dict[str, object] = Field(default_factory=dict)
    local_backup_ref: str | None = None
    remote_backup_path: str = ""
    temp_path: str | None = None
    applied: bool = False
    validation_result: str | None = None
    rollback_result: str | None = None


class ChangeSet(BaseModel):
    """A coordinated bundle of file changes that progresses through a state machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    changeset_id: str = Field(min_length=1, max_length=120)
    incident_id: str = Field(min_length=1, max_length=120)
    project_id: str = Field(min_length=1, max_length=120)
    target_id: str = Field(min_length=1, max_length=120)
    service_name: str = Field(min_length=1, max_length=120)
    files: tuple[FileChange, ...] = Field(default_factory=tuple)
    status: ChangeSetStatus = ChangeSetStatus.DRAFT
    created_at: datetime | None = None
    updated_at: datetime | None = None
    verification_plan: str = ""
    rollback_plan: str = ""
    approval_id: str | None = None
