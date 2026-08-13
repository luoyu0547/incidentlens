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

    REMOTE_SESSION_CONNECTED = "remote_session.connected"
    REMOTE_SESSION_DISCONNECTED = "remote_session.disconnected"
    REMOTE_SESSION_FAILED = "remote_session.failed"
    REMOTE_OPERATION_STARTED = "remote_operation.started"
    REMOTE_OPERATION_COMPLETED = "remote_operation.completed"

    LOG_SUBSCRIPTION_STARTED = "log.subscription_started"
    LOG_SUBSCRIPTION_PAUSED = "log.subscription_paused"
    LOG_SUBSCRIPTION_RESUMED = "log.subscription_resumed"
    LOG_SUBSCRIPTION_DELETED = "log.subscription_deleted"
    LOG_BATCH_WRITTEN = "log.batch_written"
    LOG_SOURCE_ROTATED = "log.source_rotated"
    LOG_BACKPRESSURE = "log.backpressure"
    LOG_SUBSCRIPTION_ERROR = "log.subscription_error"

    INVESTIGATION_CREATED = "investigation.created"
    INVESTIGATION_STARTED = "investigation.started"
    INVESTIGATION_STATUS_CHANGED = "investigation.status_changed"
    INVESTIGATION_COMPLETED = "investigation.completed"
    INVESTIGATION_CANCELLED = "investigation.cancelled"
    INVESTIGATION_FAILED = "investigation.failed"

    AGENT_RUN_STARTED = "agent_run.started"
    AGENT_RUN_STATUS_CHANGED = "agent_run.status_changed"
    AGENT_RUN_COMPLETED = "agent_run.completed"
    AGENT_RUN_FAILED = "agent_run.failed"
    AGENT_RUN_CANCELLED = "agent_run.cancelled"

    TOOL_CALL_STARTED = "tool_call.started"
    TOOL_CALL_STATUS_CHANGED = "tool_call.status_changed"
    TOOL_CALL_COMPLETED = "tool_call.completed"

    CHILD_RUN_STARTED = "child_run.started"
    CHILD_RUN_COMPLETED = "child_run.completed"

    EVIDENCE_APPENDED = "evidence.appended"

    REGISTRY_PROPOSAL_CREATED = "registry_proposal.created"
    REGISTRY_PROPOSAL_DECIDED = "registry_proposal.decided"

    RECOVERY_STARTED = "recovery.started"
    RECOVERY_COMPLETED = "recovery.completed"


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
