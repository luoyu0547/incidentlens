"""Authenticated durable agent-session facade routes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from incidentlens_control_plane.agent_sessions.service import (
    AgentSessionForbidden,
    AgentSessionService,
)
from incidentlens_control_plane.agent_sessions.store import AgentSessionNotFound
from incidentlens_control_plane.agent_sessions.types import (
    AgentMessageAccepted,
    AgentMessageCreate,
    AgentMessageView,
    AgentSessionCreate,
    AgentSessionPatch,
    AgentSessionView,
)
from incidentlens_control_plane.api.idempotency import (
    execute_idempotent,
    idempotency_request_sha256,
)
from incidentlens_control_plane.api.models import ApiErrorResponse
from incidentlens_control_plane.auth.dependencies import (
    authorize_target,
    get_principal,
    require_scopes,
)
from incidentlens_control_plane.auth.types import Principal, PrincipalScope
from incidentlens_control_plane.targets.store import TargetNotFound

router = APIRouter(
    prefix="/api/v1/agent-sessions",
    tags=["agent-sessions"],
    dependencies=[Depends(get_principal)],
)

_CREATE_ROUTE = "/api/v1/agent-sessions"
_ITEM_ROUTE = "/api/v1/agent-sessions/{session_id}"
_MESSAGE_ROUTE = "/api/v1/agent-sessions/{session_id}/messages"
_CANCEL_ROUTE = "/api/v1/agent-sessions/{session_id}/cancel"
_RESUME_ROUTE = "/api/v1/agent-sessions/{session_id}/resume"


def _errors(description: str) -> dict[int, object]:
    return {
        401: {"model": ApiErrorResponse, "description": "Authentication required"},
        403: {"model": ApiErrorResponse, "description": "Permission denied"},
        404: {"model": ApiErrorResponse, "description": description},
        409: {"model": ApiErrorResponse, "description": "Conflict"},
        422: {"model": ApiErrorResponse, "description": "Validation or idempotency failure"},
    }


def _service(request: Request) -> AgentSessionService:
    return request.app.state.runtime.agent_sessions


def _not_found(exc: Exception) -> HTTPException:
    if isinstance(exc, (AgentSessionNotFound, AgentSessionForbidden, TargetNotFound)):
        return HTTPException(status_code=404, detail="Agent session not found")
    return HTTPException(status_code=500, detail="Internal server error")


def _view(service: AgentSessionService, session_id: str, principal: Principal) -> AgentSessionView:
    return service.to_view(service.get_owned(session_id, principal.principal_id))


@router.post(
    "",
    status_code=201,
    response_model=AgentSessionView,
    operation_id="createAgentSession",
    responses=_errors("Session not found"),
)
async def create_session(
    request: Request,
    response: Response,
    body: AgentSessionCreate,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.OPERATE))],
) -> AgentSessionView:
    authorize_target(principal, body.target_id)
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    digest = idempotency_request_sha256(
        method="POST", route_key=_CREATE_ROUTE, path_params={}, canonical_body=canonical
    )

    async def action() -> tuple[int, AgentSessionView]:
        # Resolve the facade target before persisting a session.
        request.app.state.runtime.target_service.get_target(body.target_id)
        created = _service(request).create_session(
            principal_id=principal.principal_id,
            target_id=body.target_id,
            title=body.title,
            service_id=body.service_id,
            now=datetime.now(UTC),
        )
        return 201, _service(request).to_view(created)

    try:
        _, payload, replayed = await execute_idempotent(
            service=request.app.state.runtime.idempotency,
            principal=principal,
            method="POST",
            route_key=_CREATE_ROUTE,
            idempotency_key=request.headers.get("Idempotency-Key"),
            request_sha256=digest,
            response_type=AgentSessionView,
            action=action,
        )
    except Exception as exc:
        raise _not_found(exc) from exc
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return payload


@router.get(
    "",
    response_model=list[AgentSessionView],
    operation_id="listAgentSessions",
    responses=_errors(""),
)
async def list_sessions(
    request: Request,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
) -> list[AgentSessionView]:
    return [
        _service(request).to_view(item)
        for item in _service(request).list_owned(principal.principal_id)
    ]


@router.get(
    "/{session_id}",
    response_model=AgentSessionView,
    operation_id="getAgentSession",
    responses=_errors("Session not found"),
)
async def get_session(
    request: Request,
    session_id: str,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
) -> AgentSessionView:
    try:
        return _view(_service(request), session_id, principal)
    except Exception as exc:
        raise _not_found(exc) from exc


@router.patch(
    "/{session_id}",
    response_model=AgentSessionView,
    operation_id="patchAgentSession",
    responses=_errors("Session not found"),
)
async def patch_session(
    request: Request,
    response: Response,
    session_id: str,
    body: AgentSessionPatch,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.OPERATE))],
) -> AgentSessionView:
    try:
        _service(request).get_owned(session_id, principal.principal_id)
    except Exception as exc:
        raise _not_found(exc) from exc
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    digest = idempotency_request_sha256(
        method="PATCH",
        route_key=_ITEM_ROUTE,
        path_params={"session_id": session_id},
        canonical_body=canonical,
    )

    async def action() -> tuple[int, AgentSessionView]:
        updated = _service(request).patch_session(
            session_id, principal.principal_id, body, now=datetime.now(UTC)
        )
        return 200, _service(request).to_view(updated)

    _, payload, replayed = await execute_idempotent(
        service=request.app.state.runtime.idempotency,
        principal=principal,
        method="PATCH",
        route_key=_ITEM_ROUTE,
        idempotency_key=request.headers.get("Idempotency-Key"),
        request_sha256=digest,
        response_type=AgentSessionView,
        action=action,
    )
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return payload


@router.post(
    "/{session_id}/messages",
    status_code=202,
    response_model=AgentMessageAccepted,
    operation_id="sendAgentMessage",
    responses=_errors("Session not found"),
)
async def send_message(
    request: Request,
    response: Response,
    session_id: str,
    body: AgentMessageCreate,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.OPERATE))],
) -> AgentMessageAccepted:
    try:
        _service(request).get_owned(session_id, principal.principal_id)
    except Exception as exc:
        raise _not_found(exc) from exc
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    digest = idempotency_request_sha256(
        method="POST",
        route_key=_MESSAGE_ROUTE,
        path_params={"session_id": session_id},
        canonical_body=canonical,
    )

    async def action() -> tuple[int, AgentMessageAccepted]:
        accepted = _service(request).accept_message(
            session_id=session_id,
            principal_id=principal.principal_id,
            content=body.content,
            now=datetime.now(UTC),
        )
        return 202, accepted

    _, payload, replayed = await execute_idempotent(
        service=request.app.state.runtime.idempotency,
        principal=principal,
        method="POST",
        route_key=_MESSAGE_ROUTE,
        idempotency_key=request.headers.get("Idempotency-Key"),
        request_sha256=digest,
        response_type=AgentMessageAccepted,
        action=action,
    )
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return payload


@router.get(
    "/{session_id}/messages",
    response_model=list[AgentMessageView],
    operation_id="listAgentMessages",
    responses=_errors("Session not found"),
)
async def list_messages(
    request: Request,
    session_id: str,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
    after_message_id: str | None = None,
    limit: int = 100,
) -> list[AgentMessageView]:
    try:
        return list(
            _service(request).list_messages(
                session_id, principal.principal_id, after_message_id=after_message_id, limit=limit
            )
        )
    except Exception as exc:
        raise _not_found(exc) from exc


@router.post(
    "/{session_id}/cancel",
    response_model=AgentSessionView,
    operation_id="cancelAgentSession",
    responses=_errors("Session not found"),
)
async def cancel_session(
    request: Request,
    response: Response,
    session_id: str,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.OPERATE))],
) -> AgentSessionView:
    return await _lifecycle(request, response, session_id, principal, "cancel")


@router.post(
    "/{session_id}/resume",
    status_code=202,
    response_model=AgentMessageAccepted,
    operation_id="resumeAgentSession",
    responses=_errors("Session not found"),
)
async def resume_session(
    request: Request,
    response: Response,
    session_id: str,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.OPERATE))],
) -> AgentMessageAccepted:
    try:
        _service(request).get_owned(session_id, principal.principal_id)
    except Exception as exc:
        raise _not_found(exc) from exc
    digest = idempotency_request_sha256(
        method="POST",
        route_key=_RESUME_ROUTE,
        path_params={"session_id": session_id},
        canonical_body="{}",
    )

    async def action() -> tuple[int, AgentMessageAccepted]:
        return 202, _service(request).resume(
            session_id, principal.principal_id, now=datetime.now(UTC)
        )

    _, payload, replayed = await execute_idempotent(
        service=request.app.state.runtime.idempotency,
        principal=principal,
        method="POST",
        route_key=_RESUME_ROUTE,
        idempotency_key=request.headers.get("Idempotency-Key"),
        request_sha256=digest,
        response_type=AgentMessageAccepted,
        action=action,
    )
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return payload


async def _lifecycle(
    request: Request, response: Response, session_id: str, principal: Principal, action_name: str
) -> AgentSessionView:
    try:
        _service(request).get_owned(session_id, principal.principal_id)
    except Exception as exc:
        raise _not_found(exc) from exc
    route_key = _CANCEL_ROUTE if action_name == "cancel" else _RESUME_ROUTE
    digest = idempotency_request_sha256(
        method="POST",
        route_key=route_key,
        path_params={"session_id": session_id},
        canonical_body="{}",
    )

    async def action() -> tuple[int, AgentSessionView]:
        session = (
            _service(request).cancel(session_id, principal.principal_id)
            if action_name == "cancel"
            else _service(request).resume(session_id, principal.principal_id)
        )
        return 200, _service(request).to_view(session)

    _, payload, replayed = await execute_idempotent(
        service=request.app.state.runtime.idempotency,
        principal=principal,
        method="POST",
        route_key=route_key,
        idempotency_key=request.headers.get("Idempotency-Key"),
        request_sha256=digest,
        response_type=AgentSessionView,
        action=action,
    )
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return payload


__all__ = ["router"]
