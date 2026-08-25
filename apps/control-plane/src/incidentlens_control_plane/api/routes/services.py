"""Authenticated service detail read projection (``GET /api/v1/services/{id}``)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from incidentlens_control_plane.api.models import ApiErrorResponse
from incidentlens_control_plane.auth.dependencies import get_principal, require_scopes
from incidentlens_control_plane.auth.types import Principal, PrincipalScope
from incidentlens_control_plane.projections.types import ServiceDetailView

router = APIRouter(
    prefix="/api/v1/services",
    tags=["services"],
    dependencies=[Depends(get_principal)],
)


def _error(description: str) -> dict[str, object]:
    return {"model": ApiErrorResponse, "description": description}


@router.get(
    "/{service_id}",
    response_model=ServiceDetailView,
    operation_id="getService",
    responses={
        401: _error("Authentication required"),
        403: _error("Permission denied"),
        404: _error("Service not found"),
    },
)
async def get_service(
    request: Request,
    service_id: str,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
) -> ServiceDetailView:
    view = request.app.state.runtime.service_projection.read_service(
        service_id,
        allowed_target_ids=principal.allowed_target_ids,
    )
    if view is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return view


__all__ = ["router"]
