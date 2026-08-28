"""Authenticated evidence detail read route."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from incidentlens_control_plane.api.models import ApiErrorResponse
from incidentlens_control_plane.auth.dependencies import get_principal, require_scopes
from incidentlens_control_plane.auth.types import Principal, PrincipalScope
from incidentlens_control_plane.projections.evidence import EvidenceDetailView

router = APIRouter(
    prefix="/api/v1/evidence",
    tags=["evidence"],
    dependencies=[Depends(get_principal)],
)


def _error(description: str) -> dict[str, object]:
    return {"model": ApiErrorResponse, "description": description}


@router.get(
    "/{evidence_ref_id}",
    response_model=EvidenceDetailView,
    operation_id="getEvidence",
    responses={
        401: _error("Authentication required"),
        403: _error("Permission denied"),
        404: _error("Evidence not found"),
    },
)
async def get_evidence(
    request: Request,
    evidence_ref_id: str,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
) -> EvidenceDetailView:
    view = request.app.state.runtime.evidence_projection.get_evidence(
        evidence_ref_id,
        allowed_target_ids=principal.allowed_target_ids,
    )
    if view is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return view


__all__ = ["router"]
