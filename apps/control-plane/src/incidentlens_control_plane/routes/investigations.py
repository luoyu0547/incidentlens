"""Investigation HTTP API routes.

Only structured, already-redacted domain contracts are serialized: requests
reject unknown fields (``extra="forbid"``) so host/user/credential/provider-secret
fields can never be accepted, and responses expose IDs, statuses, counts and
bounded summaries only.  Lifecycle actions route through
``InvestigationService`` so the event stream and the store stay consistent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.evidence.types import EvidenceKind
from incidentlens_control_plane.investigation.service import (
    InvestigationAlreadyTerminal,
    NotAcceptingInvestigations,
    TooManyActiveInvestigations,
)
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.store import (
    AgentRunNotFound,
    AlreadyExists,
    IllegalTransition,
    InvestigationNotFound,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentScope,
    InvestigationBudget,
    RegistryProposalStatus,
    ToolCall,
)
from incidentlens_control_plane.routes import get_runtime

router = APIRouter(prefix="/api/investigations", tags=["investigations"])


class CreateInvestigationRequest(BaseModel):
    """Body to create a new investigation.  No host/user/credential fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)
    service: str = Field(min_length=1, max_length=120)
    symptom: str = Field(min_length=1, max_length=2_000)
    incident_id: str | None = Field(default=None, min_length=1, max_length=120)
    budget: InvestigationBudget | None = None


class StartInvestigationRequest(BaseModel):
    """Body to start (or resume) an investigation with a parent-run scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: AgentScope
    budget: AgentBudget | None = None


class ResumeInvestigationRequest(BaseModel):
    """Body naming the agent run to resume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_run_id: str = Field(min_length=1, max_length=120)


class ToolCallView(BaseModel):
    """A tool call as exposed by the API.

    ``arguments`` (the raw tool input, which may carry file content or command
    text) is deliberately omitted; callers see the tool name, status, evidence
    ids and redacted error only.
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
    output_bytes: int = Field(ge=0)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=24)
    approval_id: str | None = None
    error_redacted: str | None = None


def _tool_call_view(tool_call: ToolCall) -> ToolCallView:
    return ToolCallView(
        tool_call_id=tool_call.tool_call_id,
        agent_run_id=tool_call.agent_run_id,
        tool_name=tool_call.tool_name,
        status=tool_call.status,
        idempotency_key=tool_call.idempotency_key,
        planned_at=tool_call.planned_at,
        started_at=tool_call.started_at,
        finished_at=tool_call.finished_at,
        output_bytes=tool_call.output_bytes,
        evidence_ids=tool_call.evidence_ids,
        approval_id=tool_call.approval_id,
        error_redacted=tool_call.error_redacted,
    )


def _investigation_not_found(investigation_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Investigation not found: {investigation_id}")


def _run_not_found(agent_run_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Agent run not found: {agent_run_id}")


@router.post("", status_code=201)
async def create_investigation(
    request: Request, body: CreateInvestigationRequest
) -> dict[str, Any]:
    """Create a new investigation in the CREATED state."""
    runtime = get_runtime(request)
    try:
        record = runtime.investigations.create_investigation(
            project_id=body.project_id,
            target_id=body.target_id,
            service=body.service,
            symptom=body.symptom,
            incident_id=body.incident_id,
            budget=body.budget,
        )
    except AlreadyExists:
        raise HTTPException(status_code=409, detail="Investigation already exists")
    except TooManyActiveInvestigations as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except NotAcceptingInvestigations as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return record.model_dump(mode="json")


@router.get("")
async def list_investigations(
    request: Request,
    project_id: str | None = Query(default=None, max_length=80),
    status: InvestigationStatus | None = Query(default=None),
    incident_id: str | None = Query(default=None, max_length=120),
) -> list[dict[str, Any]]:
    """List investigations, optionally filtered."""
    runtime = get_runtime(request)
    records = runtime.investigations.list_investigations(
        project_id=project_id, status=status, incident_id=incident_id
    )
    return [record.model_dump(mode="json") for record in records]


@router.get("/{investigation_id}")
async def get_investigation(request: Request, investigation_id: str) -> dict[str, Any]:
    """Return one investigation."""
    runtime = get_runtime(request)
    try:
        record = runtime.investigations.get_investigation(investigation_id)
    except InvestigationNotFound:
        raise _investigation_not_found(investigation_id)
    return record.model_dump(mode="json")


@router.post("/{investigation_id}/start")
async def start_investigation(
    request: Request,
    investigation_id: str,
    body: StartInvestigationRequest,
) -> dict[str, Any]:
    """Start (or resume) the investigation's parent run."""
    runtime = get_runtime(request)
    try:
        run = await runtime.investigations.start(
            investigation_id, body.scope, parent_budget=body.budget
        )
    except InvestigationNotFound:
        raise _investigation_not_found(investigation_id)
    except InvestigationAlreadyTerminal:
        raise HTTPException(
            status_code=409, detail=f"Investigation {investigation_id} is already terminal"
        )
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return run.model_dump(mode="json")


@router.post("/{investigation_id}/cancel")
async def cancel_investigation(
    request: Request, investigation_id: str
) -> dict[str, Any]:
    """Request cancellation of an investigation (idempotent)."""
    runtime = get_runtime(request)
    try:
        record = await runtime.investigations.cancel(investigation_id)
    except InvestigationNotFound:
        raise _investigation_not_found(investigation_id)
    return record.model_dump(mode="json")


@router.post("/{investigation_id}/resume")
async def resume_investigation(
    request: Request,
    investigation_id: str,
    body: ResumeInvestigationRequest,
) -> dict[str, Any]:
    """Resume a paused agent run belonging to the investigation."""
    runtime = get_runtime(request)
    try:
        run = runtime.investigations.get_run(body.agent_run_id)
    except AgentRunNotFound:
        raise _run_not_found(body.agent_run_id)
    if run.investigation_id != investigation_id:
        raise _run_not_found(body.agent_run_id)
    resumed = await runtime.investigations.resume_run(body.agent_run_id)
    return resumed.model_dump(mode="json")


@router.get("/{investigation_id}/runs")
async def list_runs(
    request: Request,
    investigation_id: str,
    status: AgentRunStatus | None = Query(default=None),
) -> list[dict[str, Any]]:
    """List agent runs for an investigation, optionally filtered by status."""
    runtime = get_runtime(request)
    try:
        runtime.investigations.get_investigation(investigation_id)
    except InvestigationNotFound:
        raise _investigation_not_found(investigation_id)
    records = runtime.investigations.list_runs(
        investigation_id=investigation_id, status=status
    )
    return [record.model_dump(mode="json") for record in records]


@router.get("/{investigation_id}/runs/{agent_run_id}")
async def get_run(
    request: Request, investigation_id: str, agent_run_id: str
) -> dict[str, Any]:
    """Return one agent run belonging to the investigation."""
    runtime = get_runtime(request)
    try:
        record = runtime.investigations.get_run(agent_run_id)
    except AgentRunNotFound:
        raise _run_not_found(agent_run_id)
    if record.investigation_id != investigation_id:
        raise _run_not_found(agent_run_id)
    return record.model_dump(mode="json")


@router.get("/{investigation_id}/runs/{agent_run_id}/children")
async def list_run_children(
    request: Request, investigation_id: str, agent_run_id: str
) -> list[dict[str, Any]]:
    """List the container children delegated by a parent run."""
    runtime = get_runtime(request)
    try:
        parent = runtime.investigations.get_run(agent_run_id)
    except AgentRunNotFound:
        raise _run_not_found(agent_run_id)
    if parent.investigation_id != investigation_id:
        raise _run_not_found(agent_run_id)
    records = runtime.investigations.list_children(
        parent_run_id=agent_run_id, investigation_id=investigation_id
    )
    return [record.model_dump(mode="json") for record in records]


@router.get("/{investigation_id}/runs/{agent_run_id}/tool-calls")
async def list_run_tool_calls(
    request: Request,
    investigation_id: str,
    agent_run_id: str,
    status: ToolCallStatus | None = Query(default=None),
) -> list[dict[str, Any]]:
    """List tool calls for an agent run, optionally filtered by status."""
    runtime = get_runtime(request)
    try:
        run = runtime.investigations.get_run(agent_run_id)
    except AgentRunNotFound:
        raise _run_not_found(agent_run_id)
    if run.investigation_id != investigation_id:
        raise _run_not_found(agent_run_id)
    records = runtime.investigation_store.list_tool_calls(
        agent_run_id=agent_run_id,
        status=status,
    )
    return [_tool_call_view(record).model_dump(mode="json") for record in records]


@router.get("/{investigation_id}/runs/{agent_run_id}/checkpoints")
async def list_run_checkpoints(
    request: Request, investigation_id: str, agent_run_id: str
) -> list[dict[str, Any]]:
    """List append-only checkpoints for an agent run, oldest first."""
    runtime = get_runtime(request)
    try:
        run = runtime.investigations.get_run(agent_run_id)
    except AgentRunNotFound:
        raise _run_not_found(agent_run_id)
    if run.investigation_id != investigation_id:
        raise _run_not_found(agent_run_id)
    records = runtime.investigations.list_checkpoints(agent_run_id)
    return [record.model_dump(mode="json") for record in records]


@router.get("/{investigation_id}/runs/{agent_run_id}/rounds")
async def list_run_rounds(
    request: Request, investigation_id: str, agent_run_id: str
) -> list[dict[str, Any]]:
    """List round summaries for an agent run, oldest first."""
    runtime = get_runtime(request)
    try:
        run = runtime.investigations.get_run(agent_run_id)
    except AgentRunNotFound:
        raise _run_not_found(agent_run_id)
    if run.investigation_id != investigation_id:
        raise _run_not_found(agent_run_id)
    records = runtime.investigations.list_rounds(agent_run_id)
    return [record.model_dump(mode="json") for record in records]


@router.get("/{investigation_id}/runs/{agent_run_id}/delegated-tasks")
async def list_run_delegated_tasks(
    request: Request, investigation_id: str, agent_run_id: str
) -> list[dict[str, Any]]:
    """List delegated-task packages persisted by a parent run."""
    runtime = get_runtime(request)
    try:
        run = runtime.investigations.get_run(agent_run_id)
    except AgentRunNotFound:
        raise _run_not_found(agent_run_id)
    if run.investigation_id != investigation_id:
        raise _run_not_found(agent_run_id)
    records = runtime.investigations.list_delegated_tasks(
        parent_run_id=agent_run_id, investigation_id=investigation_id
    )
    return [record.model_dump(mode="json") for record in records]


@router.get("/{investigation_id}/hypotheses")
async def list_hypotheses(
    request: Request,
    investigation_id: str,
    agent_run_id: str | None = Query(default=None, max_length=120),
) -> list[dict[str, Any]]:
    """List hypotheses proposed within an investigation."""
    runtime = get_runtime(request)
    try:
        runtime.investigations.get_investigation(investigation_id)
    except InvestigationNotFound:
        raise _investigation_not_found(investigation_id)
    records = runtime.investigations.list_hypotheses(
        investigation_id=investigation_id, agent_run_id=agent_run_id
    )
    return [record.model_dump(mode="json") for record in records]


@router.get("/{investigation_id}/conclusions")
async def list_conclusions(
    request: Request,
    investigation_id: str,
    agent_run_id: str | None = Query(default=None, max_length=120),
) -> list[dict[str, Any]]:
    """List grounded conclusions reached within an investigation."""
    runtime = get_runtime(request)
    try:
        runtime.investigations.get_investigation(investigation_id)
    except InvestigationNotFound:
        raise _investigation_not_found(investigation_id)
    records = runtime.investigations.list_conclusions(
        investigation_id=investigation_id, agent_run_id=agent_run_id
    )
    return [record.model_dump(mode="json") for record in records]


@router.get("/{investigation_id}/proposals")
async def list_proposals(
    request: Request,
    investigation_id: str,
    status: RegistryProposalStatus | None = Query(default=None),
) -> list[dict[str, Any]]:
    """List registry-update proposals for an investigation."""
    runtime = get_runtime(request)
    try:
        runtime.investigations.get_investigation(investigation_id)
    except InvestigationNotFound:
        raise _investigation_not_found(investigation_id)
    records = runtime.investigations.list_proposals(
        investigation_id=investigation_id, status=status
    )
    return [record.model_dump(mode="json") for record in records]


@router.get("/{investigation_id}/evidence")
async def list_investigation_evidence(
    request: Request,
    investigation_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    agent_run_id: str | None = Query(default=None, max_length=120),
    kind: EvidenceKind | None = Query(default=None),
) -> list[dict[str, Any]]:
    """List redacted evidence collected during an investigation.

    Evidence is keyed by the investigation's incident id; the ``agent_run_id``
    filter narrows to one run.  Only already-redacted, bounded content is
    returned.
    """
    runtime = get_runtime(request)
    try:
        investigation = runtime.investigations.get_investigation(investigation_id)
    except InvestigationNotFound:
        raise _investigation_not_found(investigation_id)
    records = runtime.evidence.query(
        incident_id=investigation.incident_id,
        agent_run_id=agent_run_id,
        evidence_kind=kind,
        limit=limit,
    )
    return [record.model_dump(mode="json") for record in records]
