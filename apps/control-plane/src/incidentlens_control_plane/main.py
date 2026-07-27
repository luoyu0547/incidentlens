"""Control plane FastAPI application.

Provides:
  - POST /api/telemetry/events — receive and persist TelemetryEvent
  - GET /healthz — health check
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from incidentlens_telemetry.database import create_engine
from incidentlens_telemetry.repository import TelemetryRepository

from incidentlens_control_plane.routes.telemetry import router as telemetry_router
from incidentlens_control_plane.routes.telemetry import set_repository

app = FastAPI(title="IncidentLens Control Plane", version="0.1.0")

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
