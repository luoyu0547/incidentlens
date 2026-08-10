"""State used to bound an investigation model without exposing hidden reasoning."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EvidenceReference(BaseModel):
    """An immutable reference to a result collected by a typed adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1, max_length=120)
    operation_id: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=2_000)


class InvestigationState(BaseModel):
    """The minimum auditable state supplied to the next model turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str = Field(min_length=1, max_length=120)
    target_id: str = Field(min_length=1, max_length=80)
    service: str = Field(min_length=1, max_length=120)
    symptom: str = Field(min_length=1, max_length=2_000)
    round_number: int = Field(default=0, ge=0)
    max_rounds: int = Field(default=8, ge=1, le=20)
    tool_calls: int = Field(default=0, ge=0)
    max_tool_calls: int = Field(default=16, ge=1, le=50)
    evidence: tuple[EvidenceReference, ...] = ()


class ProposedConclusion(BaseModel):
    """A model conclusion that is valid only when it cites current evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=4_000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=12)
