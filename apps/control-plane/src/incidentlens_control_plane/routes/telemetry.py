"""Telemetry API routes for the control plane.

Provides:
  - POST /api/telemetry/events — receive and persist TelemetryEvent
"""

from __future__ import annotations

from fastapi import APIRouter, status
from incidentlens_contracts.models import TelemetryEvent
from incidentlens_telemetry.repository import TelemetryRepository

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])

# Repository is set by main.py during app startup
_repository: TelemetryRepository | None = None


def set_repository(repository: TelemetryRepository) -> None:
    """Set the telemetry repository for the routes."""
    global _repository
    _repository = repository


@router.post("/events", status_code=status.HTTP_201_CREATED)
async def receive_telemetry_event(event: TelemetryEvent) -> dict[str, str]:
    """Receive a TelemetryEvent and persist it to the telemetry store."""
    if _repository is None:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "message": "repository not configured"},
        )
    _repository.record(event)
    return {"status": "recorded"}
