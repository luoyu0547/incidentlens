"""Remote session lifecycle HTTP API routes.

Connection parameters are never accepted from the client.  A connect request
names a registered project and target; the target's host, username, port,
allowed paths, and credentials are resolved only from the project registry.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType
from incidentlens_control_plane.project_registry.store import ProjectNotFound
from incidentlens_control_plane.routes import get_runtime
from incidentlens_control_plane.runtime import RuntimeServices

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/remote-sessions", tags=["remote-sessions"])


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)


class ContainerSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=80)
    service: str = Field(min_length=1, max_length=120)
    container: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class HostSessionView(BaseModel):
    """Public host-session view; never exposes a transport or credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    target_id: str
    project_id: str
    connected_at: datetime
    status: str = "connected"


class ContainerSessionView(BaseModel):
    """Public container-session view; never exposes a process object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    parent_session_id: str
    container: str
    status: str = "connected"


async def _emit_event(
    runtime: RuntimeServices,
    event_type: RuntimeEventType,
    payload: dict[str, object],
) -> None:
    event = RuntimeEvent(
        event_id=uuid.uuid4().hex,
        sequence=0,
        event_type=event_type,
        occurred_at=datetime.now(UTC),
        payload=payload,
    )
    stored_event = runtime.events.append(event)
    await runtime.broker.publish(stored_event)


@router.post("", status_code=201)
async def connect_session(
    request: Request, body: ConnectRequest
) -> JSONResponse:
    """Open (or reuse) a persistent host session for a registered target."""
    runtime = get_runtime(request)

    try:
        record = runtime.projects.get(body.project_id)
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="Project not found")

    target = next(
        (t for t in record.targets if t.target_id == body.target_id), None
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found in project")

    existing = await runtime.sessions.find_live(body.target_id)
    if existing is not None:
        session = existing
        status_code = 200
    else:
        try:
            session = await runtime.sessions.connect(target)
        except Exception:
            logger.exception(
                "remote connection failed for project=%s target=%s",
                body.project_id,
                body.target_id,
            )
            await _emit_event(
                runtime,
                RuntimeEventType.REMOTE_SESSION_FAILED,
                {
                    "project_id": body.project_id,
                    "target_id": body.target_id,
                    "reason": "connection failed",
                },
            )
            raise HTTPException(
                status_code=502, detail="Remote connection failed"
            ) from None
        status_code = 201
        await _emit_event(
            runtime,
            RuntimeEventType.REMOTE_SESSION_CONNECTED,
            {
                "session_id": session.session_id,
                "project_id": body.project_id,
                "target_id": body.target_id,
            },
        )

    view = HostSessionView(
        session_id=session.session_id,
        target_id=session.target_id,
        project_id=body.project_id,
        connected_at=session.connected_at,
        status="connected",
    )
    return JSONResponse(status_code=status_code, content=view.model_dump(mode="json"))


@router.post("/{host_session_id}/containers", status_code=201)
async def spawn_container_session(
    request: Request, host_session_id: str, body: ContainerSessionRequest
) -> JSONResponse:
    """Spawn a fresh container exec session as a child of a host session."""
    runtime = get_runtime(request)

    host = runtime.sessions.get_host_session(host_session_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Host session not found")

    try:
        record = runtime.projects.get(body.project_id)
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="Project not found")

    if host.target_id not in {target.target_id for target in record.targets}:
        raise HTTPException(
            status_code=409, detail="Host session target is not in the project"
        )

    service = next(
        (s for s in record.services if s.compose_service == body.service), None
    )
    if service is None:
        raise HTTPException(status_code=404, detail="Service not found in project")
    if body.container not in service.container_names:
        raise HTTPException(
            status_code=409, detail="Container is not registered for the service"
        )

    await _emit_event(
        runtime,
        RuntimeEventType.REMOTE_OPERATION_STARTED,
        {
            "kind": "container_session",
            "parent_session_id": host_session_id,
            "target_id": host.target_id,
            "service": body.service,
            "container": body.container,
        },
    )
    started = datetime.now(UTC)
    child = await runtime.sessions.spawn_container_session(
        host_session_id, body.container
    )
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    await _emit_event(
        runtime,
        RuntimeEventType.REMOTE_OPERATION_COMPLETED,
        {
            "kind": "container_session",
            "session_id": child.session_id,
            "parent_session_id": host_session_id,
            "target_id": host.target_id,
            "service": body.service,
            "container": body.container,
            "duration_ms": duration_ms,
        },
    )

    view = ContainerSessionView(
        session_id=child.session_id,
        parent_session_id=child.parent_session_id,
        container=child.container,
        status="connected",
    )
    return JSONResponse(status_code=201, content=view.model_dump(mode="json"))


@router.get("/{session_id}")
async def session_status(request: Request, session_id: str) -> dict[str, Any]:
    """Report session status with connection health, never a transport object."""
    runtime = get_runtime(request)

    host = runtime.sessions.get_host_session(session_id)
    if host is not None:
        alive = await host.transport.is_alive()
        return {
            "session_id": host.session_id,
            "target_id": host.target_id,
            "status": "connected" if alive else "stale",
            "connected_at": host.connected_at.isoformat(),
        }

    child = runtime.sessions.get_container_session(session_id)
    if child is not None:
        return {
            "session_id": child.session_id,
            "parent_session_id": child.parent_session_id,
            "container": child.container,
            "status": "connected",
        }

    raise HTTPException(status_code=404, detail="Session not found")


@router.delete("/{session_id}", status_code=204)
async def delete_session(request: Request, session_id: str) -> Response:
    """Close a session by ID. Idempotent; a child never disconnects its host."""
    runtime = get_runtime(request)

    child = runtime.sessions.get_container_session(session_id)
    if child is not None:
        await runtime.sessions.close_container_session(session_id)
        await _emit_event(
            runtime,
            RuntimeEventType.REMOTE_SESSION_DISCONNECTED,
            {
                "session_id": session_id,
                "parent_session_id": child.parent_session_id,
                "container": child.container,
            },
        )
        return Response(status_code=204)

    host = runtime.sessions.get_host_session(session_id)
    if host is not None:
        await runtime.sessions.disconnect(host.target_id)
        await _emit_event(
            runtime,
            RuntimeEventType.REMOTE_SESSION_DISCONNECTED,
            {
                "session_id": session_id,
                "target_id": host.target_id,
            },
        )
        return Response(status_code=204)

    # Unknown or already-closed sessions delete idempotently.
    return Response(status_code=204)
