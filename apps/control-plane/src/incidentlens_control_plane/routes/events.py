"""Event stream HTTP API routes."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect

from incidentlens_control_plane.runtime import RuntimeServices

router = APIRouter(prefix="/api/events", tags=["events"])


def _get_runtime(request: Request) -> RuntimeServices:
    """Extract runtime services from request state."""
    return cast(RuntimeServices, request.app.state.runtime)


@router.get("")
async def list_events(
    request: Request,
    after: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    """List runtime events after a given sequence."""
    runtime = _get_runtime(request)
    events = runtime.events.list_after(after, limit=limit)
    return [event.model_dump(mode="json") for event in events]


@router.websocket("/ws")
async def websocket_events(
    websocket: WebSocket,
    after: int = Query(0, ge=0),
) -> None:
    """WebSocket endpoint for real-time event streaming.

    Replays historical events from the durable store, then streams
    live events from the broker.
    """
    await websocket.accept()
    runtime = cast(RuntimeServices, websocket.app.state.runtime)

    try:
        # Subscribe to live events before replay to avoid race conditions
        async with runtime.broker.subscribe() as queue:
            # Replay historical events
            historical_events = runtime.events.list_after(after, limit=1000)
            max_sequence = after
            for event in historical_events:
                await websocket.send_json(event.model_dump(mode="json"))
                max_sequence = max(max_sequence, event.sequence)

            # Stream live events
            while True:
                try:
                    event = await queue.get()
                    # Skip events already sent during replay
                    if event.sequence > max_sequence:
                        await websocket.send_json(event.model_dump(mode="json"))
                        max_sequence = event.sequence
                except asyncio.TimeoutError:
                    continue
    except WebSocketDisconnect:
        pass
