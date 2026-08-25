"""Authenticated investigation summary read routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from incidentlens_control_plane.api.errors import ApiProblem
from incidentlens_control_plane.api.models import ApiErrorResponse
from incidentlens_control_plane.auth.dependencies import (
    authorize_target,
    get_principal,
    require_scopes,
)
from incidentlens_control_plane.auth.types import Principal, PrincipalScope
from incidentlens_control_plane.projections.investigations import (
    InvestigationSummaryPage,
    InvestigationSummaryView,
    decode_investigation_cursor,
)

router = APIRouter(
    prefix="/api/v1/investigations",
    tags=["investigations"],
    dependencies=[Depends(get_principal)],
)


def _error(description: str) -> dict[str, object]:
    return {"model": ApiErrorResponse, "description": description}


@router.get(
    "",
    response_model=InvestigationSummaryPage,
    operation_id="listInvestigationSummaries",
    responses={
        401: _error("Authentication required"),
        403: _error("Permission denied"),
        422: _error("Invalid investigation cursor or filter"),
    },
)
async def list_investigation_summaries(
    request: Request,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
    status: str | None = Query(default=None),
    target_id: str | None = Query(default=None),
    service_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    after: str | None = Query(default=None),
) -> InvestigationSummaryPage:
    if target_id is not None:
        authorize_target(principal, target_id)
    try:
        cursor = decode_investigation_cursor(after)
    except ValueError as exc:
        raise ApiProblem(
            status_code=422,
            code="cursor_invalid",
            message="investigation cursor is invalid",
        ) from exc
    return request.app.state.runtime.investigation_summary_projection.list_summaries(
        allowed_target_ids=principal.allowed_target_ids,
        target_id=target_id,
        service_id=service_id,
        status=status,
        limit=limit,
        after=cursor,
    )


@router.get(
    "/{investigation_id}/summary",
    response_model=InvestigationSummaryView,
    operation_id="getInvestigationSummary",
    responses={
        401: _error("Authentication required"),
        403: _error("Permission denied"),
        404: _error("Investigation not found"),
    },
)
async def get_investigation_summary(
    request: Request,
    investigation_id: str,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
) -> InvestigationSummaryView:
    view = request.app.state.runtime.investigation_summary_projection.get_summary(
        investigation_id,
        allowed_target_ids=principal.allowed_target_ids,
    )
    if view is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return view


__all__ = ["router"]
