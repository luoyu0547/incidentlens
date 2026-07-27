"""SSE events route for streaming investigation updates.

Provides:
  - GET /api/investigations/{incident_id}/events — SSE stream of investigation events

Event types: state_changed, tool_called, evidence_recorded, report_ready
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from incidentlens_control_plane.events import EventBus

logger = logging.getLogger("incidentlens_control_plane.events")

router = APIRouter(prefix="/api/investigations", tags=["events"])

# Global event bus — set by main.py during app startup
_event_bus: EventBus | None = None


def set_event_bus(bus: EventBus) -> None:
    """Set the event bus for the SSE route."""
    global _event_bus
    _event_bus = bus


async def _event_generator(
    incident_id: str,
    request: Request,
    bus: EventBus,
) -> Any:
    """Generate SSE events for a given investigation.

    Yields events from the event bus until the client disconnects.
    Sends a heartbeat every 15 seconds to keep the connection alive.
    """
    subscriber = bus.subscribe(incident_id)
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(subscriber.__anext__(), timeout=15.0)
                yield event
            except asyncio.TimeoutError:
                # Send heartbeat comment to keep connection alive
                yield ": heartbeat\n\n"
    except Exception as e:
        logger.warning(f"SSE event stream error: {e}")
    finally:
        bus.unsubscribe(incident_id, subscriber)


@router.get("/{incident_id}/events", response_model=None)
async def investigation_events(
    incident_id: str, request: Request
) -> EventSourceResponse | JSONResponse:
    """Stream SSE events for a specific investigation.

    Event types: state_changed, tool_called, evidence_recorded, report_ready
    """
    if _event_bus is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "Event bus not configured"},
        )

    return EventSourceResponse(
        _event_generator(incident_id, request, _event_bus),
    )
