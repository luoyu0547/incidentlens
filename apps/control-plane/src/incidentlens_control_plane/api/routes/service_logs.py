"""Product service log history route.

``GET /api/v1/services/{service_id}/logs`` pages durable, redacted log records
for one service using opaque product cursors.  The route authenticates the
caller, requires READ scope, applies the principal's target restriction in SQL,
and rejects malformed or non-product cursors without ever resuming from row
zero.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from incidentlens_control_plane.api.errors import ApiProblem
from incidentlens_control_plane.api.models import ApiErrorResponse
from incidentlens_control_plane.auth.dependencies import get_principal, require_scopes
from incidentlens_control_plane.auth.types import Principal, PrincipalScope
from incidentlens_control_plane.logs.cursors import decode_log_cursor
from incidentlens_control_plane.logs.types import LogSeverity
from incidentlens_control_plane.logs.views import LogPage, list_log_page
from incidentlens_control_plane.targets.store import TargetNotFound

router = APIRouter(
    prefix="/api/v1/services/{service_id}/logs",
    tags=["service-logs"],
    dependencies=[Depends(get_principal)],
)


def _error(description: str) -> dict[int, object]:
    return {
        401: {"model": ApiErrorResponse, "description": "Authentication required"},
        403: {"model": ApiErrorResponse, "description": "Permission denied"},
        422: {"model": ApiErrorResponse, "description": description},
    }


def _decode_cursor(value: str | None, name: str) -> int | None:
    if value is None:
        return None
    try:
        return decode_log_cursor(value)
    except ValueError as exc:
        raise ApiProblem(
            status_code=422,
            code="cursor_invalid",
            message=f"{name} cursor is invalid",
        ) from exc


@router.get(
    "",
    response_model=LogPage,
    operation_id="listServiceLogs",
    responses=_error("Invalid log cursor or filter"),
)
async def list_service_logs(
    request: Request,
    service_id: str,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
    before: str | None = Query(default=None),
    after: str | None = Query(default=None),
    snapshot: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    severity: LogSeverity | None = Query(default=None),
    source_ref: str | None = Query(default=None),
) -> LogPage:
    before_sequence = _decode_cursor(before, "before")
    after_sequence = _decode_cursor(after, "after")
    snapshot_sequence = _decode_cursor(snapshot, "snapshot")
    if before_sequence is not None and after_sequence is not None:
        raise ApiProblem(
            status_code=422,
            code="cursor_invalid",
            message="before and after cursors are mutually exclusive",
        )
    runtime = request.app.state.runtime
    try:
        target_service = runtime.target_service
        if not any(
            service.service == service_id
            for target in target_service.list_targets()
            for service in target_service.services_for_target(target.target_id)
            if principal.authorized_for(target.target_id)
        ):
            raise TargetNotFound(service_id)
    except (TargetNotFound, StopIteration) as exc:
        raise ApiProblem(
            status_code=404,
            code="resource_not_found",
            message="Service not found",
        ) from exc
    store = runtime.log_store
    return list_log_page(
        store,
        service_name=service_id,
        before_sequence=before_sequence,
        after_sequence=after_sequence,
        snapshot_sequence=snapshot_sequence,
        limit=limit,
        severity=severity,
        source_ref=source_ref,
        allowed_target_ids=principal.allowed_target_ids,
    )


__all__ = ["router"]
