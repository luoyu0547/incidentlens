"""Authenticated issue read projection routes."""

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
from incidentlens_control_plane.projections.issues import (
    IssuePage,
    IssueStatus,
    IssueView,
    decode_issue_cursor,
)

router = APIRouter(
    prefix="/api/v1/issues",
    tags=["issues"],
    dependencies=[Depends(get_principal)],
)


def _error(description: str) -> dict[str, object]:
    return {"model": ApiErrorResponse, "description": description}


@router.get(
    "",
    response_model=IssuePage,
    operation_id="listIssues",
    responses={
        401: _error("Authentication required"),
        403: _error("Permission denied"),
        422: _error("Invalid issue cursor or filter"),
    },
)
async def list_issues(
    request: Request,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
    status: IssueStatus | None = Query(default=None),
    target_id: str | None = Query(default=None),
    service_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    after: str | None = Query(default=None),
) -> IssuePage:
    if target_id is not None:
        authorize_target(principal, target_id)
    try:
        cursor = decode_issue_cursor(after)
    except ValueError as exc:
        raise ApiProblem(
            status_code=422,
            code="cursor_invalid",
            message="issue cursor is invalid",
        ) from exc
    return request.app.state.runtime.issue_projection.list_issues(
        allowed_target_ids=principal.allowed_target_ids,
        target_id=target_id,
        service_id=service_id,
        status=status,
        limit=limit,
        after=cursor,
    )


@router.get(
    "/{issue_id}",
    response_model=IssueView,
    operation_id="getIssue",
    responses={
        401: _error("Authentication required"),
        403: _error("Permission denied"),
        404: _error("Issue not found"),
    },
)
async def get_issue(
    request: Request,
    issue_id: str,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
) -> IssueView:
    view = request.app.state.runtime.issue_projection.get_issue(
        issue_id,
        allowed_target_ids=principal.allowed_target_ids,
    )
    if view is None:
        raise HTTPException(status_code=404, detail="Issue not found")
    return view


__all__ = ["router"]
