"""Phase 4 investigation domain contracts.

All contracts are immutable (``frozen=True``) and reject unknown fields
(``extra="forbid"``) so a model can never smuggle hidden reasoning, raw
transcripts or out-of-scope references into a persisted record. Anything the
model cites must be an ``EvidenceReference`` that the run actually collected;
cross-investigation or fabricated citations are rejected by the guard, not the
schema.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    HypothesisStatus,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.logs.types import LogScope


def _validate_unique_citations(
    cls: type[object], value: tuple[str, ...]
) -> tuple[str, ...]:
    """Reject empty-string or duplicate evidence citations."""
    if any(not citation.strip() for citation in value):
        raise ValueError("evidence_ids must not contain empty strings")
    if len(value) != len(set(value)):
        raise ValueError("evidence_ids must be unique")
    return value


def _validate_absolute_simple_paths(
    cls: type[object], value: tuple[PurePosixPath, ...]
) -> tuple[PurePosixPath, ...]:
    """Reject non-absolute paths and any ``..`` traversal."""
    for path in value:
        if not path.is_absolute():
            raise ValueError("paths must be absolute")
        if ".." in path.parts:
            raise ValueError("paths must not contain '..'")
    return value


def _json_compatible(value: Any) -> bool:
    """Return True when ``value`` is a JSON-serializable plain structure."""
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, list):
        return all(_json_compatible(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _json_compatible(item)
            for key, item in value.items()
        )
    return False


class EvidenceReference(BaseModel):
    """An immutable reference to a result collected by a typed adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1, max_length=120)
    operation_id: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=2_000)


class StopReason(StrEnum):
    COMPLETED = "completed"
    BUDGET_ROUNDS = "budget_rounds"
    BUDGET_TOOL_CALLS = "budget_tool_calls"
    BUDGET_TIME = "budget_time"
    BUDGET_OUTPUT = "budget_output"
    BUDGET_EVIDENCE = "budget_evidence"
    BUDGET_CHILDREN = "budget_children"
    BUDGET_NO_NEW_EVIDENCE = "budget_no_new_evidence"
    MISSING_EVIDENCE = "missing_evidence"
    PENDING_APPROVAL = "pending_approval"
    UNCERTAIN_STATE = "uncertain_state"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AgentRunKind(StrEnum):
    PARENT = "parent"
    CHILD = "child"


class ChildReportStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class RegistryUpdateKind(StrEnum):
    CONTAINER_REGISTRATION = "container_registration"
    PATH_EXTENSION = "path_extension"


class RegistryProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    STALE = "stale"


class InvestigationBudget(BaseModel):
    """Global budgets that bound the whole investigation across all runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_rounds: int = Field(default=32, ge=1, le=1_000)
    max_tool_calls: int = Field(default=64, ge=1, le=2_000)
    max_children: int = Field(default=4, ge=0, le=32)
    max_wall_clock_seconds: int = Field(default=7_200, ge=1, le=86_400)
    max_total_output_bytes: int = Field(
        default=16 * 1024 * 1024, ge=1, le=512 * 1024 * 1024
    )
    max_evidence: int = Field(default=300, ge=1, le=10_000)
    max_no_new_evidence_rounds: int = Field(default=3, ge=1, le=20)


class AgentBudget(BaseModel):
    """Budgets that bound a single parent or child run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_rounds: int = Field(default=8, ge=1, le=200)
    max_tool_calls: int = Field(default=16, ge=1, le=500)
    max_wall_clock_seconds: int = Field(default=1_800, ge=1, le=43_200)
    max_output_bytes_per_tool: int = Field(
        default=512 * 1024, ge=1, le=64 * 1024 * 1024
    )
    max_total_output_bytes: int = Field(
        default=4 * 1024 * 1024, ge=1, le=128 * 1024 * 1024
    )
    max_evidence: int = Field(default=100, ge=1, le=5_000)
    max_no_new_evidence_rounds: int = Field(default=3, ge=1, le=20)


class UsageCounters(BaseModel):
    """Cumulative consumption counters for a run or the whole investigation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rounds: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    children: int = Field(default=0, ge=0)
    wall_clock_seconds: int = Field(default=0, ge=0)
    total_output_bytes: int = Field(default=0, ge=0)
    evidence_count: int = Field(default=0, ge=0)
    consecutive_no_new_evidence_rounds: int = Field(default=0, ge=0)


class ProviderUsage(BaseModel):
    """Usage reported for a single provider turn, bounded before model output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    output_bytes: int = Field(default=0, ge=0)


class AgentScope(BaseModel):
    """Host or container-scoped bounds that isolate a parent or child run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)
    scope: LogScope
    service_name: str | None = Field(default=None, min_length=1, max_length=120)
    container_name: str | None = Field(default=None, min_length=1, max_length=120)
    allowed_host_paths: tuple[PurePosixPath, ...] = ()
    allowed_container_paths: tuple[PurePosixPath, ...] = ()

    @model_validator(mode="after")
    def scope_identity_must_be_consistent(self) -> AgentScope:
        if self.scope is LogScope.CONTAINER:
            if not self.service_name:
                raise ValueError("container scope requires service_name")
            if not self.container_name:
                raise ValueError("container scope requires container_name")
        else:
            if self.service_name is not None:
                raise ValueError("host scope must not set service_name")
            if self.container_name is not None:
                raise ValueError("host scope must not set container_name")
        return self

    _validate_allowed_host_paths = field_validator("allowed_host_paths")(
        _validate_absolute_simple_paths
    )
    _validate_allowed_container_paths = field_validator("allowed_container_paths")(
        _validate_absolute_simple_paths
    )


class Hypothesis(BaseModel):
    """A structured hypothesis that only cites evidence from its own run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str = Field(min_length=1, max_length=120)
    agent_run_id: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=4_000)
    facts: tuple[str, ...] = Field(default=(), max_length=32)
    inferences: tuple[str, ...] = Field(default=(), max_length=32)
    unknowns: tuple[str, ...] = Field(default=(), max_length=32)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=24)
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    created_at: datetime
    updated_at: datetime

    _validate_evidence_ids = field_validator("evidence_ids")(_validate_unique_citations)


class Conclusion(BaseModel):
    """A grounded conclusion whose citations must resolve to run evidence.

    ``evidence_ids`` may be empty so a model can surface that it cannot ground
    a conclusion; the guard treats an empty citation set as a missing-evidence
    stop rather than a fabricated one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=4_000)
    facts: tuple[str, ...] = Field(default=(), max_length=32)
    inferences: tuple[str, ...] = Field(default=(), max_length=32)
    unknowns: tuple[str, ...] = Field(default=(), max_length=32)
    limitations: tuple[str, ...] = Field(default=(), max_length=32)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=12)

    _validate_evidence_ids = field_validator("evidence_ids")(_validate_unique_citations)


class ChildReport(BaseModel):
    """Evidence-grounded report a child run returns to its parent.

    A partial report records the evidence collected so far plus the reason the
    child stopped (crash, budget, cancellation) so the parent never receives a
    fabricated or out-of-scope story.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_run_id: str = Field(min_length=1, max_length=120)
    parent_run_id: str = Field(min_length=1, max_length=120)
    status: ChildReportStatus
    summary: str = Field(min_length=1, max_length=4_000)
    findings: tuple[str, ...] = Field(max_length=64)
    inferences: tuple[str, ...] = Field(default=(), max_length=32)
    unknowns: tuple[str, ...] = Field(default=(), max_length=32)
    limitations: tuple[str, ...] = Field(default=(), max_length=32)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=24)
    stop_reason: StopReason
    created_at: datetime

    _validate_evidence_ids = field_validator("evidence_ids")(_validate_unique_citations)


class DelegatedTaskPackage(BaseModel):
    """Scoped, bounded context a parent hands to a child run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    child_run_id: str = Field(min_length=1, max_length=120)
    parent_run_id: str = Field(min_length=1, max_length=120)
    investigation_id: str = Field(min_length=1, max_length=120)
    task_prompt: str = Field(min_length=1, max_length=4_000)
    scope: AgentScope
    budget: AgentBudget
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=24)

    _validate_evidence_ids = field_validator("evidence_ids")(_validate_unique_citations)


class ToolCall(BaseModel):
    """A single planned or executed tool invocation for an agent run.

    ``arguments`` is persisted verbatim so an approved tool call can be
    re-executed by the approval-decision handler with the exact same inputs;
    the JSON-compatible validator keeps raw/non-serializable values out of the
    store.  Event payloads never include ``arguments``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: str = Field(min_length=1, max_length=120)
    agent_run_id: str = Field(min_length=1, max_length=120)
    tool_name: str = Field(min_length=1, max_length=120)
    status: ToolCallStatus
    idempotency_key: str = Field(min_length=1, max_length=200)
    planned_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output_bytes: int = Field(default=0, ge=0)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=24)
    approval_id: str | None = Field(default=None, max_length=120)
    error_redacted: str | None = Field(default=None, max_length=2_000)
    arguments: dict[str, Any] = Field(default_factory=dict)

    _validate_evidence_ids = field_validator("evidence_ids")(_validate_unique_citations)

    @field_validator("arguments")
    @classmethod
    def _arguments_must_be_json_compatible(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not _json_compatible(value):
            raise ValueError("arguments must be JSON-compatible plain values")
        return value


class Checkpoint(BaseModel):
    """A point-in-time snapshot used to resume a run after a restart."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: str = Field(min_length=1, max_length=120)
    agent_run_id: str = Field(min_length=1, max_length=120)
    sequence: int = Field(ge=1)
    status: AgentRunStatus
    round_number: int = Field(ge=0)
    usage: UsageCounters
    created_at: datetime


class RegistryUpdateProposal(BaseModel):
    """Evidence-backed proposal to widen registry scope, pending approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str = Field(min_length=1, max_length=120)
    investigation_id: str = Field(min_length=1, max_length=120)
    agent_run_id: str = Field(min_length=1, max_length=120)
    kind: RegistryUpdateKind
    discovery_evidence_id: str = Field(min_length=1, max_length=120)
    proposed_project_id: str = Field(min_length=1, max_length=80)
    proposed_target_id: str = Field(min_length=1, max_length=80)
    proposed_service_name: str = Field(min_length=1, max_length=120)
    proposed_container_name: str = Field(min_length=1, max_length=120)
    proposed_paths: tuple[PurePosixPath, ...] = ()
    approval_intent_sha256: str | None = Field(default=None, min_length=1, max_length=64)
    status: RegistryProposalStatus = RegistryProposalStatus.PENDING
    created_at: datetime
    decided_at: datetime | None = None

    _validate_proposed_paths = field_validator("proposed_paths")(
        _validate_absolute_simple_paths
    )


class Investigation(BaseModel):
    """The top-level auditable entity spanning parent and child runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: str = Field(min_length=1, max_length=120)
    incident_id: str = Field(min_length=1, max_length=120)
    project_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)
    service: str = Field(min_length=1, max_length=120)
    symptom: str = Field(min_length=1, max_length=2_000)
    status: InvestigationStatus
    budget: InvestigationBudget
    usage: UsageCounters
    stop_reason: StopReason | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class AgentRun(BaseModel):
    """A single parent or child bounded loop within an investigation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_run_id: str = Field(min_length=1, max_length=120)
    investigation_id: str = Field(min_length=1, max_length=120)
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=120)
    kind: AgentRunKind
    scope: AgentScope
    status: AgentRunStatus
    budget: AgentBudget
    usage: UsageCounters
    stop_reason: StopReason | None = None
    evidence: tuple[EvidenceReference, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def kind_must_match_parent_link(self) -> AgentRun:
        if self.kind is AgentRunKind.PARENT and self.parent_run_id is not None:
            raise ValueError("parent run must not set parent_run_id")
        if self.kind is AgentRunKind.CHILD and self.parent_run_id is None:
            raise ValueError("child run requires parent_run_id")
        return self
