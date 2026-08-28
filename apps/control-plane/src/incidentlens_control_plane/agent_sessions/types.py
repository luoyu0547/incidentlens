"""Wire and persistence types for the Agent Session product facade.

The session is a *facade*, never a second agent state machine: it maps one
Investigation's status into the session status, and the durable
``agent_messages`` table is a redacted projection of the user prompts plus the
assistant's validated transcript text.  Tool arguments/results and hidden
provider reasoning never reach this table.

All on-the-wire models are frozen and reject unknown fields so the product
contract is exactly the declared shape and actor identity can never be smuggled
through a request body.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentSessionStatus(StrEnum):
    """The client-facing session lifecycle, projected from Investigation status."""

    IDLE = "idle"
    ACTIVE = "active"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"


class AgentMessageRole(StrEnum):
    """Roles that may appear in a projected product message."""

    USER = "user"
    ASSISTANT = "assistant"


class AgentSessionCreate(BaseModel):
    """Body for ``POST /api/v1/agent-sessions``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(min_length=1, max_length=120)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    service_id: str | None = Field(default=None, min_length=1, max_length=120)


class AgentSessionPatch(BaseModel):
    """Body for ``PATCH /api/v1/agent-sessions/{id}`` (title only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str | None = Field(default=None, min_length=1, max_length=200)


class AgentMessageCreate(BaseModel):
    """Body for ``POST /api/v1/agent-sessions/{id}/messages``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str = Field(min_length=1, max_length=20_000)


class AgentMessageAccepted(BaseModel):
    """The fast 202 acceptance for a queued agent message.

    The message is acked immediately; the durable ``operation_id`` lets the
    caller follow execution on the operations read surface.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    operation_id: str
    accepted: Literal[True] = True


class AgentSessionView(BaseModel):
    """The product-facing representation of an agent session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    target_id: str
    service_id: str | None
    title: str | None
    status: AgentSessionStatus
    investigation_id: str | None
    owner: str
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value


class AgentMessageView(BaseModel):
    """One projected product message (redacted text only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    session_id: str
    role: AgentMessageRole
    content: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value


class AgentSession(BaseModel):
    """A persisted ``agent_sessions`` row (internal, not exposed verbatim)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    target_id: str
    service_id: str | None
    title: str | None
    owner: str
    investigation_id: str | None
    status: AgentSessionStatus
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value


class AgentMessage(BaseModel):
    """A persisted ``agent_messages`` row (redacted text only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    session_id: str
    investigation_id: str | None
    agent_run_id: str | None
    role: AgentMessageRole
    content_redacted: str
    transcript_sequence: int | None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value


__all__ = [
    "AgentMessage",
    "AgentMessageAccepted",
    "AgentMessageCreate",
    "AgentMessageRole",
    "AgentMessageView",
    "AgentSession",
    "AgentSessionCreate",
    "AgentSessionPatch",
    "AgentSessionStatus",
    "AgentSessionView",
]
