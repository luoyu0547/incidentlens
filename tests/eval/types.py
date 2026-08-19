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
    DelegatedTaskPackage,
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
    mutation_tool_call_ids: tuple[str, ...] = ()
    expected_child_run_ids: tuple[str, ...] = ()
    delegation_forms: tuple[str, ...] = ()
    aggregate_sources: tuple[str, ...] = ()
    source_runs: tuple[AgentRun, ...] = ()
    source_investigations: tuple[Investigation, ...] = ()
    delegated_tasks: tuple[DelegatedTaskPackage, ...] = ()
    owned_evidence_by_run: dict[str, tuple[EvidenceRef, ...]] = Field(default_factory=dict)
    elapsed_seconds: float = Field(default=0.0, ge=0.0)

    @classmethod
    def from_live_result(cls, result: object) -> HarnessTrace:
        """Adapt a callable live result using only its persisted records.

        The adapter deliberately performs model validation for every record; it
        does not infer runtime state from provider responses or test fixtures.
        """
        records = getattr(result, "to_record")()
        run = AgentRun.model_validate(records["run"])
        investigation = Investigation.model_validate(records["investigation"])
        rounds = tuple(AgentRound.model_validate(item) for item in records["rounds"])
        tool_calls = tuple(ToolCall.model_validate(item) for item in records["tool_calls"])
        transcript = tuple(
            TranscriptMessage.model_validate(item) for item in records["transcript"]
        )
        boundaries = tuple(
            CompactBoundary.model_validate(item) for item in records["compact_boundaries"]
        )
        evidence = tuple(EvidenceReference.model_validate(item) for item in records["evidence"])
        conclusions = tuple(Conclusion.model_validate(item) for item in records["conclusions"])
        hooks = tuple(RuntimeEvent.model_validate(item) for item in records["hooks"])
        mutation_ids = tuple(
            call.tool_call_id
            for call in tool_calls
            if call.tool_name in {"file_write", "file_edit", "docker_action", "shell_exec"}
        )
        return cls(
            scenario="real_maas",
            investigation=investigation,
            run=run,
            rounds=rounds,
            tool_calls=tool_calls,
            transcript=transcript,
            compact_boundaries=boundaries,
            evidence=evidence,
            conclusions=conclusions,
            hook_events=hooks,
            mutation_tool_call_ids=mutation_ids,
            elapsed_seconds=0.0,
        )


__all__ = ["EvidenceRef", "HarnessEvalResult", "HarnessTrace"]
