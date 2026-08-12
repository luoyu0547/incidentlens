"""Evidence HTTP API routes.

Evidence is built exclusively from stored redacted ``LogRecord`` rows and the
immutable ``EvidenceRef`` views are serialized directly, so raw content never
appears in responses.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.evidence.types import EvidenceKind
from incidentlens_control_plane.routes import get_runtime

router = APIRouter(prefix="/api/evidence", tags=["evidence"])
incidents_router = APIRouter(prefix="/api/incidents", tags=["evidence"])


class CreateEvidenceFromLogRecordsRequest(BaseModel):
    """Body naming a stored incident and the redacted records to cite."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str = Field(min_length=1, max_length=120)
    log_ids: tuple[str, ...]
    created_by: str = Field(min_length=1, max_length=80)


@router.post("/from-log-records", status_code=201)
async def create_evidence_from_log_records(
    request: Request, body: CreateEvidenceFromLogRecordsRequest
) -> list[dict[str, object]]:
    """Create immutable evidence refs from stored redacted log records."""
    runtime = get_runtime(request)
    refs: list[dict[str, object]] = []
    for log_id in body.log_ids:
        record = runtime.log_store.get_record(log_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Log record not found: {log_id}")
        ref = runtime.evidence.create_from_log_record(
            record,
            incident_id=body.incident_id,
            created_by=body.created_by,
            now=datetime.now(UTC),
        )
        refs.append(ref.model_dump(mode="json"))
    return refs


@router.get("/{evidence_ref_id}")
async def get_evidence(request: Request, evidence_ref_id: str) -> dict[str, object]:
    """Return a single immutable evidence ref."""
    runtime = get_runtime(request)
    try:
        ref = runtime.evidence.get(evidence_ref_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return ref.model_dump(mode="json")


@incidents_router.get("/{incident_id}/evidence")
async def list_incident_evidence(
    request: Request,
    incident_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    kind: EvidenceKind | None = None,
    agent_run_id: str | None = Query(default=None, max_length=120),
) -> list[dict[str, object]]:
    """List evidence refs for an incident, oldest first.

    Optional ``kind`` and ``agent_run_id`` filters narrow the result set; both
    default to "any" so existing callers are unaffected.
    """
    runtime = get_runtime(request)
    refs = runtime.evidence.query(
        incident_id=incident_id,
        evidence_kind=kind,
        agent_run_id=agent_run_id,
        limit=limit,
    )
    return [ref.model_dump(mode="json") for ref in refs]
