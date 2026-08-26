"""Authenticated workspace invalidation SSE endpoint.

``GET /events/v1/workspace`` authenticates the caller (bearer token or signed
session cookie, both read-only so no CSRF applies), authorizes the optional
``target_id`` filter, then hands the connection to
:class:`~incidentlens_control_plane.streams.workspace.WorkspaceEventStream` for
durable replay into the live invalidation feed.

The client resumes with either the standard ``Last-Event-ID`` header (takes
precedence) or the ``after_event_id`` query value (fallback).  A cursor that is
absent or outside retained history produces a single ``stream.gap`` frame and
ends the stream; authentication still happens before the ``StreamingResponse``
is created.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from incidentlens_control_plane.auth.dependencies import get_principal, require_scopes
from incidentlens_control_plane.auth.types import Principal, PrincipalScope
from incidentlens_control_plane.streams.workspace import WorkspaceEventStream

router = APIRouter(
    prefix="/events/v1",
    tags=["workspace-events"],
    dependencies=[Depends(get_principal)],
)


def _resolve_cursor(request: Request, after_event_id: str | None) -> str | None:
    """Pick the SSE resume cursor: header first, query value as fallback."""
    header = request.headers.get("Last-Event-ID")
    if header is not None:
        return header.strip() or None
    if after_event_id is not None and after_event_id.strip():
        return after_event_id.strip()
    return None


@router.get(
    "/workspace",
    operation_id="streamWorkspaceEvents",
    responses={
        401: {"description": "Authentication required"},
        403: {"description": "Permission denied"},
    },
)
async def workspace_events(
    request: Request,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
    target_id: str | None = Query(default=None),
    after_event_id: str | None = Query(default=None),
) -> StreamingResponse:
    """Stream safe resource invalidations for the authenticated workspace client."""
    if target_id is not None and not principal.authorized_for(target_id):
        raise HTTPException(
            status_code=403, detail="target is not allowed for this principal"
        )
    runtime = request.app.state.runtime
    stream = WorkspaceEventStream(
        events=runtime.events,
        broker=runtime.broker,
        heartbeat_seconds=runtime.settings.stream_heartbeat_seconds,
    )

    async def body():
        async for frame in stream.run(
            after_event_id=_resolve_cursor(request, after_event_id),
            target_id=target_id,
            allowed_target_ids=principal.allowed_target_ids,
        ):
            yield frame

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["router"]
