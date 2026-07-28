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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from incidentlens_control_plane.events import _global_bus
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

logger = logging.getLogger("incidentlens_control_plane")


@asynccontextmanager
async def lifespan(
    app: FastAPI,
    *,
    engine_override=None,
) -> AsyncIterator[None]:
    """Async lifespan that owns all resource creation and teardown.

    When *engine_override* is supplied (test path), install only that
    injected engine and yield without reading model config.  The production
    module calls ``create_app()`` with no override.
    """
    from incidentlens_telemetry.database import create_engine
    from incidentlens_telemetry.repository import TelemetryRepository

    if engine_override is not None:
        set_investigation_engine(engine_override)
        set_event_bus(_global_bus)
        set_investigation_event_bus(_global_bus)
        yield
        return

    # Production path: create real resources
    mode_str = os.environ.get("INCIDENTLENS_AGENT_MODE", "llm_agent")
    from incidentlens_control_plane.llm.config import RuntimeMode

    mode = RuntimeMode(mode_str)

    db_url = os.environ.get("TELEMETRY_DB_URL", "sqlite:///control_plane.db")
    db_engine = create_engine(db_url)
    telemetry_repo = TelemetryRepository(db_engine)
    from incidentlens_control_plane.tools.query import ReadOnlyToolkit

    toolkit = ReadOnlyToolkit(telemetry_repo)
    from incidentlens_control_plane.memory.repository import CaseRepository

    case_repository = CaseRepository(db_engine)
    from incidentlens_control_plane.agent.state import InvestigationAuditStore

    audit_store = InvestigationAuditStore(db_engine)

    from incidentlens_scenarios.store import ScenarioStore
    from incidentlens_control_plane.services.demo_reset import DemoResetService

    scenario_store = ScenarioStore(db_engine)
    demo_reset_service = DemoResetService(telemetry_repo, scenario_store)

    # Wire shared dependencies into route modules
    set_repository(telemetry_repo)
    set_case_repository(case_repository)
    set_scenario_store(scenario_store)
    set_demo_reset_service(demo_reset_service)
    set_event_bus(_global_bus)
    set_investigation_event_bus(_global_bus)

    if mode is RuntimeMode.LLM_AGENT:
        from incidentlens_control_plane.agent.checkpoint import (
            AgentCheckpointRuntime,
        )
        from incidentlens_control_plane.agent.factory import (
            build_investigation_engine,
        )
        from incidentlens_control_plane.agent.skills import SkillRuntime
        from incidentlens_control_plane.llm.config import load_models_config
        from incidentlens_control_plane.llm.registry import ModelRegistry

        checkpoint_path = Path(
            os.environ.get("INCIDENTLENS_CHECKPOINT_DB", "agent_checkpoints.db")
        )
        config_path = Path(
            os.environ.get("INCIDENTLENS_MODELS_CONFIG", "config/models.yaml")
        )
        models = load_models_config(config_path, os.environ)
        registry = ModelRegistry(models, os.environ)
        registry.get()  # startup validation and construction; no provider request

        skill_runtime = SkillRuntime(Path("skills"), audit_store)
        skill_runtime.validate()

        async with AgentCheckpointRuntime(checkpoint_path) as checkpoints:
            runtime = build_investigation_engine(
                mode=mode,
                telemetry_repo=telemetry_repo,
                toolkit=toolkit,
                case_repository=case_repository,
                audit_store=audit_store,
                model_registry=registry,
                checkpointer=checkpoints.saver,
                skill_runtime=skill_runtime,
            )
            set_investigation_engine(runtime)
            yield
    else:
        from incidentlens_control_plane.agent.factory import (
            build_investigation_engine,
        )

        set_investigation_engine(
            build_investigation_engine(
                mode=mode,
                telemetry_repo=telemetry_repo,
                toolkit=toolkit,
                case_repository=case_repository,
                audit_store=audit_store,
                model_registry=None,
                checkpointer=None,
                skill_runtime=None,
            )
        )
        yield


def create_app(*, engine_override=None) -> FastAPI:
    """Create and configure the FastAPI application.

    When *engine_override* is supplied the lifespan installs only that
    engine (test path).  Without an override the lifespan reads env vars
    and builds real resources (production path).
    """

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with lifespan(app, engine_override=engine_override):
            yield

    app = FastAPI(
        title="IncidentLens Control Plane",
        version="0.1.0",
        lifespan=_lifespan,
    )

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

    return app


# Production entry point — used by Uvicorn
app = create_app()
