"""Immutable contracts for persisted harness traces."""

from __future__ import annotations

from incidentlens_control_plane.events.types import RuntimeEvent
from incidentlens_control_plane.investigation.store import AgentRound
from incidentlens_control_plane.investigation.types import (
    AgentRun,
    ChildReportReceipt,
    CompactBoundary,
    Conclusion,
    EvidenceReference,
    Investigation,
    ToolCall,
    TranscriptMessage,
)
from pydantic import BaseModel, ConfigDict, Field

EvidenceRef = EvidenceReference


class HarnessEvalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: str
    grounded_completion: bool
    foreign_evidence_count: int = Field(ge=0)
    scope_policy_bypass_count: int = Field(ge=0)
    unapproved_mutation_count: int = Field(ge=0)
    tool_pairing_rate: float = Field(ge=0.0, le=1.0)
    compaction_recovered: bool | None = None
    child_exactly_once_rate: float = Field(ge=0.0, le=1.0)
    rounds: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0.0)


class HarnessTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    scenario: str
    investigation: Investigation
    run: AgentRun
    rounds: tuple[AgentRound, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    transcript: tuple[TranscriptMessage, ...] = ()
    compact_boundaries: tuple[CompactBoundary, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    conclusions: tuple[Conclusion, ...] = ()
    child_receipts: tuple[ChildReportReceipt, ...] = ()
    hook_events: tuple[RuntimeEvent, ...] = ()
    elapsed_seconds: float = Field(default=0.0, ge=0.0)


__all__ = ["EvidenceRef", "HarnessEvalResult", "HarnessTrace"]
