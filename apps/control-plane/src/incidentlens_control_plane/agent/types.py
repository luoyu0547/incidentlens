"""LangGraph agent state types and Pydantic proposal models."""

from __future__ import annotations

import operator
from typing import Annotated, Any, NotRequired

from incidentlens_contracts.models import Evidence, Hypothesis
from langchain.agents.middleware import AgentState
from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.llm.config import RuntimeMode


def merge_evidence(left: list[Evidence], right: list[Evidence]) -> list[Evidence]:
    """Merge two evidence lists, deduplicating by id (right wins on conflict)."""
    merged = {item.id: item for item in left}
    merged.update({item.id: item for item in right})
    return list(merged.values())


def merge_unique_strings(left: list[str], right: list[str]) -> list[str]:
    """Merge two string lists, preserving insertion order and deduplicating."""
    return list(dict.fromkeys([*left, *right]))


class IncidentAgentState(AgentState):
    """LangGraph execution state for the investigation agent.

    Extends the base AgentState (which provides `messages`) with
    investigation-specific fields. Annotated fields use reducers
    so that partial state updates merge correctly across nodes.
    """

    incident_id: str
    status: str
    phase: str
    alert: dict[str, Any]
    current_round: int
    max_rounds: int
    hypotheses: list[Hypothesis]
    evidence: Annotated[list[Evidence], merge_evidence]
    retrieved_cases: list[dict[str, Any]]
    loaded_skill_names: Annotated[list[str], merge_unique_strings]
    model_profile: str
    model_call_count: Annotated[int, operator.add]
    tool_call_count: Annotated[int, operator.add]
    fallback_used: bool
    report: dict[str, Any] | None
    last_error_code: NotRequired[str | None]
    last_checkpoint_id: NotRequired[str | None]


class InvestigationContext(BaseModel):
    """Immutable context passed to agent nodes at invocation time."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    incident_id: str
    mode: RuntimeMode


class RootCauseProposal(BaseModel):
    """Structured proposal emitted by the agent when it believes it
    has identified the root cause of an incident."""

    model_config = ConfigDict(extra="forbid")

    root_service: str = Field(min_length=1)
    cause_code: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    next_action: str = Field(min_length=1)
