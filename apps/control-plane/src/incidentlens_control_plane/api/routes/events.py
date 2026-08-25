"""Product event history route."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from incidentlens_control_plane.api.errors import ApiProblem
from incidentlens_control_plane.api.models import ApiErrorResponse
from incidentlens_control_plane.auth.dependencies import get_principal, require_scopes
from incidentlens_control_plane.auth.types import Principal, PrincipalScope
from incidentlens_control_plane.events.types import RuntimeEventType
from incidentlens_control_plane.streams.types import EventPage, StreamEventEnvelope

router = APIRouter(
    prefix="/api/v1/events",
    tags=["events"],
    dependencies=[Depends(get_principal)],
)


def _event_page(request: Request):
    return request.app.state.runtime.events


def _error(description: str) -> dict[int, object]:
    return {
        401: {"model": ApiErrorResponse, "description": "Authentication required"},
        403: {"model": ApiErrorResponse, "description": "Permission denied"},
        422: {"model": ApiErrorResponse, "description": description},
    }


@router.get(
    "",
    response_model=EventPage,
    operation_id="listProductEvents",
    responses=_error("Invalid event cursor or filter"),
)
async def list_product_events(
    request: Request,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    session_id: str | None = None,
    target_id: str | None = None,
    investigation_id: str | None = None,
    event_type: list[str] | None = Query(default=None),
) -> EventPage:
    if target_id is not None and not principal.authorized_for(target_id):
        raise ApiProblem(
            status_code=403,
            code="permission_denied",
            message="target is not allowed for this principal",
        )
    allowed_targets = principal.allowed_target_ids
    try:
        types = tuple(RuntimeEventType(value) for value in (event_type or ()))
    except ValueError as exc:
        raise ApiProblem(
            status_code=422,
            code="request_validation_failed",
            message="unknown event type",
        ) from exc
    store = _event_page(request)
    page = store.list_page(
        after_sequence=after_sequence,
        limit=limit,
        session_id=session_id,
        target_id=target_id,
        investigation_id=investigation_id,
        event_types=types,
        allowed_target_ids=allowed_targets,
    )
    return EventPage(
        items=tuple(_to_envelope(event) for event in page.items),
        next_after_sequence=page.next_after_sequence,
        has_more=page.has_more,
        latest_sequence=page.latest_sequence,
        earliest_available_sequence=page.earliest_available_sequence,
    )


def _to_envelope(event) -> StreamEventEnvelope:
    payload = event.payload
    return StreamEventEnvelope(
        schema_version=1,
        event_id=event.event_id,
        sequence=event.sequence,
        event_type=event.event_type.value,
        session_id=_string(payload.get("session_id")),
        target_id=_string(payload.get("target_id")),
        investigation_id=_string(payload.get("investigation_id")),
        occurred_at=event.occurred_at,
        payload=payload,
    )


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = ["router"]
