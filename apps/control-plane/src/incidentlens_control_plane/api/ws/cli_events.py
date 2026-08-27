"""Recoverable CLI event WebSocket route.

``WS /ws/v1/cli-events`` authenticates the caller (bearer token or signed
session cookie, both read-only so no CSRF applies), negotiates ``schema_version``
(only version 1 is accepted; anything else closes ``4406``), and then hands the
connection to :class:`~incidentlens_control_plane.streams.cli.CliEventStream`
for durable replay into the live feed.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from incidentlens_control_plane.api.ws.auth import resolve_ws_principal
from incidentlens_control_plane.auth.types import PrincipalScope
from incidentlens_control_plane.streams.cli import CliEventStream, EventFilter

router = APIRouter(prefix="/ws/v1/cli-events", tags=["cli-stream"])

_SUPPORTED_SCHEMA_VERSION = 1


@router.websocket("")
async def cli_events(
    websocket: WebSocket,
    schema_version: int = Query(default=1),
    after_sequence: int = Query(default=0, ge=0),
    session_id: str | None = Query(default=None),
    target_id: str | None = Query(default=None),
    investigation_id: str | None = Query(default=None),
    event_type: list[str] = Query(default=None),
) -> None:
    await websocket.accept()
    if schema_version != _SUPPORTED_SCHEMA_VERSION:
        await websocket.close(code=4406, reason="unsupported schema version")
        return
    principal = resolve_ws_principal(websocket)
    if principal is None:
        await websocket.close(code=4401, reason="authentication required")
        return
    if PrincipalScope.READ not in principal.scopes:
        await websocket.close(code=4403, reason="read scope required")
        return
    if target_id is not None and not principal.authorized_for(target_id):
        await websocket.close(code=4403, reason="target not allowed")
        return

    runtime = websocket.app.state.runtime
    if runtime is None:
        await websocket.close(code=4401, reason="runtime unavailable")
        return
    # Tool/run events are intentionally keyed by investigation rather than
    # session (they predate the product-session projection). Resolve the
    # session once at handshake so the live stream can apply an equivalent
    # investigation filter and never leak another session's tool rows.
    effective_investigation_id = investigation_id
    if session_id is not None and effective_investigation_id is None:
        try:
            session = runtime.agent_session_store.get_session(session_id)
            effective_investigation_id = session.investigation_id
        except Exception:  # noqa: BLE001 - invalid session is handled by stream auth
            effective_investigation_id = None
    stream = CliEventStream(
        events=runtime.events,
        broker=runtime.broker,
        filter=EventFilter(
            session_id=session_id,
            target_id=target_id,
            investigation_id=effective_investigation_id,
            event_types=tuple(event_type or ()),
        ),
        allowed_target_ids=principal.allowed_target_ids,
        resolve_investigation_id=lambda current_session_id: (
            runtime.agent_session_store.get_session(current_session_id).investigation_id
        ),
        heartbeat_seconds=runtime.settings.stream_heartbeat_seconds,
    )

    async def send(frame: str) -> None:
        await websocket.send_text(frame)

    async def close(code: int, reason: str) -> None:
        await websocket.close(code=code, reason=reason)

    try:
        await stream.run(
            after_sequence=after_sequence,
            send=send,
            close=close,
        )
    except WebSocketDisconnect:
        return
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - already disconnected
            pass
