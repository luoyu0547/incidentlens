"""Control plane FastAPI application.

Provides:
  - POST /api/telemetry/events — receive and persist TelemetryEvent
  - GET /healthz — health check
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from incidentlens_telemetry.database import create_engine
from incidentlens_telemetry.repository import TelemetryRepository

from incidentlens_control_plane.routes.telemetry import router as telemetry_router
from incidentlens_control_plane.routes.telemetry import set_repository

logger = logging.getLogger("incidentlens_control_plane")

app = FastAPI(title="IncidentLens Control Plane", version="0.1.0")


@app.middleware("http")
async def propagate_trace_headers(request: Request, call_next):
    """Extract X-Request-ID and X-Trace-ID from incoming requests and log them."""
    request_id = request.headers.get("x-request-id", "")
    trace_id = request.headers.get("x-trace-id", "")
    logger.info(
        "request received",
        extra={
            "x_request_id": request_id,
            "x_trace_id": trace_id,
            "method": request.method,
            "path": request.url.path,
        },
    )
    response = await call_next(request)
    # Propagate headers on the response as well
    if request_id:
        response.headers["X-Request-ID"] = request_id
    if trace_id:
        response.headers["X-Trace-ID"] = trace_id
    return response


# Default DB; override via TELEMETRY_DB_URL env var
_db_url = os.environ.get("TELEMETRY_DB_URL", "sqlite:///control_plane.db")
_engine = create_engine(_db_url)
_repository = TelemetryRepository(_engine)

# Configure the telemetry route with the repository
set_repository(_repository)

# Include routes
app.include_router(telemetry_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
