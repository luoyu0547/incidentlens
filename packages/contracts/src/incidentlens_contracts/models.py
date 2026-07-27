"""Pydantic models for IncidentLens shared data contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import AwareDatetime, BaseModel, Field

T = TypeVar("T")


class InvestigationStatus(StrEnum):
    """Status of an investigation lifecycle."""

    SCOPING = "scoping"
    INVESTIGATING = "investigating"
    VERIFYING = "verifying"
    REPORT_READY = "report_ready"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"


class HypothesisStatus(StrEnum):
    """Status of a hypothesis during investigation."""

    ACTIVE = "active"
    RULED_OUT = "ruled_out"
    CONFIRMED = "confirmed"


class TelemetryEvent(BaseModel):
    """A single telemetry event emitted by a service.

    Every event must carry a trace_id and service so that downstream
    consumers can correlate and filter observations.
    """

    event_type: str
    service: str
    trace_id: str
    occurred_at: AwareDatetime
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel, Generic[T]):
    """Unified return type for all read-only tools.

    ok=True means the tool succeeded; data carries the result.
    ok=False means the tool failed; error carries the message.
    metadata records limits, truncation, timing, and call identity.
    """

    ok: bool
    data: T | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    """A piece of evidence produced by a tool call during investigation.

    Evidence links a tool invocation to the hypotheses it supports
    or contradicts.
    """

    id: str
    source_tool: str
    tool_call_id: str
    content: dict[str, Any] = Field(default_factory=dict)
    supports_hypothesis_ids: list[str] = Field(default_factory=list)
    contradicts_hypothesis_ids: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    """A candidate hypothesis about the root cause of an incident.

    Hypotheses are created from evidence or historical cases and
    updated as new evidence is gathered.
    """

    id: str
    description: str
    confidence: float = 0.0
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    status: HypothesisStatus = HypothesisStatus.ACTIVE
    root_service: str = ""
    cause_code: str = ""
