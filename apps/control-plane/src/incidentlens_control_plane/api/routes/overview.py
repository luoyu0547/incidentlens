"""Authenticated overview read projection (``GET /api/v1/overview``)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from incidentlens_control_plane.api.models import ApiErrorResponse
from incidentlens_control_plane.auth.dependencies import get_principal, require_scopes
from incidentlens_control_plane.auth.types import Principal, PrincipalScope
from incidentlens_control_plane.projections.types import OverviewView

router = APIRouter(
    prefix="/api/v1/overview",
    tags=["overview"],
    dependencies=[Depends(get_principal)],
)


def _error(description: str) -> dict[str, object]:
    return {"model": ApiErrorResponse, "description": description}


@router.get(
    "",
    response_model=OverviewView,
    operation_id="getOverview",
    responses={
        401: _error("Authentication required"),
        403: _error("Permission denied"),
    },
)
async def get_overview(
    request: Request,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
) -> OverviewView:
    return request.app.state.runtime.overview_projection.read_overview(
        allowed_target_ids=principal.allowed_target_ids
    )


__all__ = ["router"]
