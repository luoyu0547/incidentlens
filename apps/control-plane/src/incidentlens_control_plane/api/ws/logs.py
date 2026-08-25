"""Authenticated cursor-based log WebSocket."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from incidentlens_control_plane.api.ws.auth import resolve_ws_principal
from incidentlens_control_plane.auth.types import PrincipalScope
from incidentlens_control_plane.streams.logs import CursorLogStream

router = APIRouter(prefix="/ws/v1/logs", tags=["log-stream"])
_FIRST_FRAME_TIMEOUT = 5.0


@router.websocket("")
async def logs_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    principal = resolve_ws_principal(websocket)
    if principal is None:
        await websocket.close(code=4401, reason="authentication required")
        return
    if PrincipalScope.READ not in principal.scopes:
        await websocket.close(code=4403, reason="read scope required")
        return
    try:
        first = await asyncio.wait_for(websocket.receive_json(), _FIRST_FRAME_TIMEOUT)
    except asyncio.TimeoutError:
        await websocket.close(code=4408, reason="first frame timeout")
        return
    except (WebSocketDisconnect, ValueError):
        return
    if not isinstance(first, dict) or first.get("action") != "subscribe":
        await websocket.close(code=4400, reason="subscribe required")
        return
    service_id = first.get("service_id")
    if not isinstance(service_id, str) or not service_id:
        await websocket.close(code=4400, reason="service_id required")
        return
    target_id = first.get("target_id")
    if target_id is not None and not isinstance(target_id, str):
        await websocket.close(code=4400, reason="invalid target_id")
        return
    if target_id is not None and not principal.authorized_for(target_id):
        await websocket.close(code=4403, reason="target not allowed")
        return
    runtime = websocket.app.state.runtime
    if runtime is None:
        await websocket.close(code=1011, reason="runtime unavailable")
        return
    stream = CursorLogStream(
        store=runtime.log_store,
        subscriptions=runtime.subscriptions,
        allowed_target_ids=principal.allowed_target_ids,
        heartbeat_seconds=runtime.settings.stream_heartbeat_seconds,
    )
    stream.service_id = service_id
    stream.target_id = target_id
    stream.severity = first.get("severity")
    stream.source_ref = first.get("source_ref")
    stream.last_sequence = 0
    try:
        await stream.run(
            send=websocket.send_json,
            receive=websocket.receive_json,
            close=websocket.close,
            initial=first,
        )
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close(code=1011, reason="stream failed")
        except Exception:
            pass


__all__ = ["router"]
