from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RuntimeEventType(StrEnum):
    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"
    PROJECT_DELETED = "project.deleted"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_APPROVED = "approval.approved"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_CONSUMED = "approval.consumed"

    CHANGESET_CREATED = "changeset.created"
    CHANGESET_STATUS_CHANGED = "changeset.status_changed"
    CHANGESET_ROLLED_BACK = "changeset.rolled_back"

    DOCKER_ACTION_REQUESTED = "docker_action.requested"
    DOCKER_ACTION_STARTED = "docker_action.started"
    DOCKER_ACTION_COMPLETED = "docker_action.completed"
    DOCKER_ACTION_FAILED = "docker_action.failed"


# JsonValue is a union type for JSON-serializable values
# Using Any to avoid recursion issues with Pydantic
JsonValue = str | int | float | bool | None | list[Any] | dict[str, Any]


class RuntimeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str = Field(min_length=1, max_length=120)
    sequence: int = Field(default=0, ge=0)
    event_type: RuntimeEventType
    occurred_at: datetime
    payload: dict[str, JsonValue]
