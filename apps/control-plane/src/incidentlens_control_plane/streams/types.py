"""Versioned durable stream contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from incidentlens_control_plane.api.models import JsonValue
from pydantic import BaseModel, ConfigDict


class StreamEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    event_id: str
    sequence: int
    event_type: str
    session_id: str | None = None
    target_id: str | None = None
    investigation_id: str | None = None
    occurred_at: datetime
    payload: dict[str, JsonValue]


class EventPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[StreamEventEnvelope, ...]
    next_after_sequence: int
    has_more: bool
    latest_sequence: int
    earliest_available_sequence: int


__all__ = ["EventPage", "StreamEventEnvelope"]
