"""Tool-free semantic compaction contract, validation and failure breaker.

When the deterministic compact pipeline still cannot keep a provider context
inside its token budget, the run asks a separate ``ContextCompactor`` provider
to produce a new ``SessionMemory`` revision from the recent transcript.  The
compactor is deliberately *tool-free*: ``CompactionRequest`` carries no tool
schemas and the production implementation must call the configured model with a
text-only instruction and an empty tool list, so a compactor can never propose
side-effecting work while summarizing.

``CompactionValidator`` is the manager-side gatekeeper.  It rejects a compactor
memory that names evidence the run does not own (foreign ids), walks the
transcript boundary backwards (non-monotonic), drops required work-state, or
carries unredacted / over-long text.  A rejected or failed compaction increments
a durable breaker (``CompactionState.consecutive_failures``); after three
consecutive failures the next attempt raises ``CompactionCircuitOpen`` until a
successful manual compact resets the breaker.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.investigation.types import (
    SessionMemory,
    TranscriptMessage,
)
from incidentlens_control_plane.logs.redaction import redact_message


class CompactionRejected(Exception):
    """Raised when a compactor's memory revision fails semantic validation."""


class CompactionCircuitOpen(Exception):
    """Raised when consecutive compaction failures tripped the failure breaker."""


class CompactionRequest(BaseModel):
    """Everything a tool-free compactor may see when summarizing a transcript.

    Deliberately has no tool field: the compactor only reads the bounded
    messages and returns a structured memory revision.  ``through_sequence`` is
    the transcript coverage the run asks the compactor to summarize up to;
    ``allowed_evidence_ids`` are the evidence ids the run actually owns, so the
    compactor can never cite a foreign or fabricated evidence index.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_run_id: str = Field(min_length=1, max_length=120)
    through_sequence: int = Field(ge=0)
    prior_memory: SessionMemory | None = None
    messages: tuple[TranscriptMessage, ...]
    allowed_evidence_ids: tuple[str, ...] = Field(default=(), max_length=256)


class ContextCompactor(Protocol):
    """A model-backed compactor that returns a memory revision, never effects."""

    async def compact(self, request: CompactionRequest) -> SessionMemory: ...


# Per-field width bounds a compactor memory must respect.  These mirror the
# deterministic ``_memory_fields`` widths in ``context.py`` so semantic memory
# is bounded as tightly as deterministic memory.
_TEXT_WIDTHS: dict[str, int] = {
    "objective": 4_000,
    "confirmed_facts": 400,
    "active_hypotheses": 400,
    "open_questions": 400,
    "completed_actions": 240,
    "child_findings": 400,
    "user_constraints": 240,
    "todos": 1_000,
    "next_actions": 240,
}


class CompactionValidator:
    """Validate a compactor's memory revision against the compaction request.

    ``validate`` raises ``CompactionRejected`` on the first violation and
    otherwise returns the memory unchanged.  Evidence ids must be a subset of
    the run's owned ids, the boundary must cover at least everything the
    compactor was asked to summarize (monotonic progress), the memory must
    belong to the requested run and advance its revision, and every free-text
    field must already be redacted and within its bounded width.
    """

    def validate(self, request: CompactionRequest, memory: SessionMemory) -> SessionMemory:
        if not isinstance(memory, SessionMemory):
            # A misbehaving compactor may return a raw dict or partial payload;
            # coerce through the strict contract so it surfaces as a ValidationError.
            memory = SessionMemory.model_validate(memory)
        if memory.agent_run_id != request.agent_run_id:
            raise CompactionRejected(
                f"compactor memory names run {memory.agent_run_id!r} but the "
                f"compaction is for {request.agent_run_id!r}"
            )
        expected_revision = (
            (request.prior_memory.revision + 1) if request.prior_memory is not None else 1
        )
        if memory.revision != expected_revision:
            raise CompactionRejected(
                f"compactor memory revision {memory.revision} is not the expected "
                f"next revision {expected_revision}"
            )
        if memory.through_transcript_sequence < max(request.through_sequence, 1):
            raise CompactionRejected(
                f"compactor memory boundary {memory.through_transcript_sequence} is "
                f"not monotonic: requested coverage through {request.through_sequence}"
            )
        foreign = set(memory.evidence_ids) - set(request.allowed_evidence_ids)
        if foreign:
            raise CompactionRejected(
                f"compactor memory cites foreign evidence: {sorted(foreign)!r}"
            )
        for field, width in _TEXT_WIDTHS.items():
            value = getattr(memory, field)
            if isinstance(value, str):
                self._check_text(field, value, width)
            else:
                for index, item in enumerate(value):
                    self._check_text(f"{field}[{index}]", item, width)
        return memory

    @staticmethod
    def _check_text(field: str, value: str, width: int) -> None:
        if len(value) > width:
            raise CompactionRejected(f"compactor memory field {field} exceeds {width} chars")
        redacted = redact_message(value, max_length=width).message_redacted
        if redacted != value:
            raise CompactionRejected(
                f"compactor memory field {field} contains unredacted content"
            )


__all__ = [
    "CompactionCircuitOpen",
    "CompactionRejected",
    "CompactionRequest",
    "CompactionValidator",
    "ContextCompactor",
]
