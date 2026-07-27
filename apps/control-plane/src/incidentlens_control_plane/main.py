"""Control plane FastAPI application.

Provides:
  - POST /api/telemetry/events — receive and persist TelemetryEvent
  - POST /api/investigations/start — start a new investigation
  - POST /api/investigations/{incident_id}/round — run one round
  - POST /api/investigations/{incident_id}/resume — resume investigation
  - GET /api/cases/search — search verified cases
  - POST /api/cases — save a new case
  - POST /api/cases/{case_id}/confirm — confirm a case
  - GET /api/scenarios — list all scenario definitions
  - POST /api/scenarios/{name}/enable — activate a scenario
  - POST /api/scenarios/{name}/disable — deactivate a scenario
  - POST /api/scenarios/reset — reset all scenarios and demo data
  - GET /api/scenarios/runtime/{service} — get active scenarios for a service
  - GET /healthz — health check
"""

from __future__ import annotations

import logging
import os
import pathlib

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from incidentlens_telemetry.database import create_engine
from incidentlens_telemetry.repository import TelemetryRepository

from incidentlens_control_plane.agent.engine import InvestigationEngine
from incidentlens_control_plane.events import _global_bus
from incidentlens_control_plane.memory.repository import CaseRepository
from incidentlens_control_plane.routes.cases import router as cases_router
from incidentlens_control_plane.routes.cases import set_repository as set_case_repository
from incidentlens_control_plane.routes.events import router as events_router
from incidentlens_control_plane.routes.events import set_event_bus
from incidentlens_control_plane.routes.investigations import (
    router as investigations_router,
)
from incidentlens_control_plane.routes.investigations import (
    set_engine as set_investigation_engine,
)
from incidentlens_control_plane.routes.investigations import (
    set_event_bus as set_investigation_event_bus,
)
from incidentlens_control_plane.routes.scenarios import (
    router as scenarios_router,
)
from incidentlens_control_plane.routes.scenarios import (
    set_demo_reset_service,
    set_scenario_store,
)
from incidentlens_control_plane.routes.telemetry import router as telemetry_router
from incidentlens_control_plane.routes.telemetry import set_repository
from incidentlens_control_plane.services.demo_reset import DemoResetService
from incidentlens_control_plane.tools.query import ReadOnlyToolkit
from incidentlens_scenarios.store import ScenarioStore

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
_toolkit = ReadOnlyToolkit(_repository)
_case_repository = CaseRepository(_engine)
_scenario_store = ScenarioStore(_engine)
_demo_reset_service = DemoResetService(_repository, _scenario_store)
_investigation_engine = InvestigationEngine(
    telemetry_repo=_repository,
    toolkit=_toolkit,
    case_repository=_case_repository,
)

# Configure the routes with their dependencies
set_repository(_repository)
set_investigation_engine(_investigation_engine)
set_case_repository(_case_repository)
set_scenario_store(_scenario_store)
set_demo_reset_service(_demo_reset_service)

# Configure event bus for SSE streaming — use the global singleton so that
# publishers (investigation routes) and subscribers (SSE endpoint) share
# the same bus instance.
set_event_bus(_global_bus)
set_investigation_event_bus(_global_bus)

# Include routes
app.include_router(telemetry_router)
app.include_router(investigations_router)
app.include_router(cases_router)
app.include_router(events_router)
app.include_router(scenarios_router)

# Mount static dashboard files.
# NOTE: This MUST come after all include_router() calls above because
# StaticFiles mounted at "/" with html=True acts as a catch-all — any
# request not matched by an earlier route will be served from the static
# directory. If route registration order changes, this mount must remain last.
_static_dir = pathlib.Path(__file__).parent.parent.parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
