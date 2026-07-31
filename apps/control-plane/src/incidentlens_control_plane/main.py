"""Control plane FastAPI application.

Provides:
  - POST /api/telemetry/events — receive and persist TelemetryEvent
  - POST /api/investigations/start — start a new investigation
  - POST /api/investigations/{incident_id}/round — run one round
  - POST /api/investigations/{incident_id}/resume — resume investigation
  - GET /api/investigations/{incident_id}/export — export investigation
  - GET /api/cases/search — search verified cases
  - POST /api/cases — save a new case
  - POST /api/cases/{case_id}/confirm — confirm a case
  - GET /api/scenarios — list all scenario definitions
  - POST /api/scenarios/{name}/enable — activate a scenario
  - POST /api/scenarios/{name}/disable — deactivate a scenario
  - POST /api/scenarios/reset — reset all scenarios and demo data
  - GET /api/scenarios/runtime/{service} — get active scenarios for a service
  - GET /api/evaluations/comparison — latest completed evaluation per strategy
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
from incidentlens_control_plane.routes.cases import set_case_service as set_case_service_route
from incidentlens_control_plane.routes.cases import set_repository as set_case_repository
from incidentlens_control_plane.routes.cases import set_retriever as set_case_retriever
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
from incidentlens_control_plane.routes.investigations import (
    set_case_service as set_investigation_case_service,
)
from incidentlens_control_plane.routes.investigations import (
    set_export_service as set_investigation_export_service,
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
from incidentlens_control_plane.routes.evaluations import router as evaluations_router

logger = logging.getLogger("incidentlens_control_plane")


@asynccontextmanager
async def lifespan(
    app: FastAPI,
    *,
    engine_override=None,
    case_service_override=None,
    retriever_override=None,
) -> AsyncIterator[None]:
    """Async lifespan that owns all resource creation and teardown.

    When overrides are supplied (test path), install only the injected
    dependencies and yield without reading model config.  The production
    module calls ``create_app()`` with no overrides.
    """
    from incidentlens_telemetry.database import create_engine
    from incidentlens_telemetry.repository import TelemetryRepository

    if engine_override is not None or case_service_override is not None:
        set_investigation_engine(engine_override)
        set_event_bus(_global_bus)
        set_investigation_event_bus(_global_bus)

        if case_service_override is not None:
            set_case_service_route(case_service_override)
            set_investigation_case_service(case_service_override)

            # Build and wire the export service for test path
            from incidentlens_control_plane.services.investigation_export import (
                InvestigationExportService,
            )
            from incidentlens_control_plane.agent.state import InvestigationAuditStore

            # Use the engine's audit store if available, otherwise create a minimal one
            audit_store = getattr(engine_override, "audit_store", None)
            if audit_store is None:
                from incidentlens_telemetry.database import create_engine as ce
                _eng = ce("sqlite:///:memory:")
                audit_store = InvestigationAuditStore(_eng)

            export_svc = InvestigationExportService(
                engine=engine_override,
                audit_store=audit_store,
                case_service=case_service_override,
            )
            set_investigation_export_service(export_svc)

        if retriever_override is not None:
            set_case_retriever(retriever_override)

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

    # Build and wire case governance services
    from incidentlens_control_plane.memory.service import CaseService

    case_svc = CaseService(case_repository)
    set_case_service_route(case_svc)
    set_investigation_case_service(case_svc)

    # Build and wire the hybrid retriever
    from incidentlens_control_plane.memory.retrieval import HybridCaseRetriever

    retriever = HybridCaseRetriever(case_repository)
    set_case_retriever(retriever)

    # Build and wire the export service
    from incidentlens_control_plane.services.investigation_export import (
        InvestigationExportService,
    )

    export_svc = InvestigationExportService(
        engine=None,  # will be set after engine is built
        audit_store=audit_store,
        case_service=case_svc,
    )
    set_investigation_export_service(export_svc)

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
            # Update export service with the real engine
            export_svc._engine = runtime
            yield
    else:
        from incidentlens_control_plane.agent.factory import (
            build_investigation_engine,
        )

        runtime = build_investigation_engine(
            mode=mode,
            telemetry_repo=telemetry_repo,
            toolkit=toolkit,
            case_repository=case_repository,
            audit_store=audit_store,
            model_registry=None,
            checkpointer=None,
            skill_runtime=None,
        )
        set_investigation_engine(runtime)
        # Update export service with the real engine
        export_svc._engine = runtime
        yield


def create_app(
    *,
    engine_override=None,
    case_service_override=None,
    retriever_override=None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    When overrides are supplied the lifespan installs only those
    injected services (test path).  Without overrides the lifespan
    reads env vars and builds real resources (production path).
    """

    # Test applications may be exercised through ASGI transports that do not
    # run lifespan hooks. Install explicit overrides eagerly so those apps
    # still expose the same injected service contract.
    if engine_override is not None:
        set_investigation_engine(engine_override)
        set_event_bus(_global_bus)
        set_investigation_event_bus(_global_bus)

    if case_service_override is not None:
        set_case_service_route(case_service_override)
        set_investigation_case_service(case_service_override)

        # Build and wire the export service eagerly for test path
        from incidentlens_control_plane.services.investigation_export import (
            InvestigationExportService,
        )
        from incidentlens_control_plane.agent.state import InvestigationAuditStore

        audit_store = getattr(engine_override, "audit_store", None) if engine_override else None
        if audit_store is None:
            from incidentlens_telemetry.database import create_engine as ce
            _eng = ce("sqlite:///:memory:")
            audit_store = InvestigationAuditStore(_eng)

        export_svc = InvestigationExportService(
            engine=engine_override,
            audit_store=audit_store,
            case_service=case_service_override,
        )
        set_investigation_export_service(export_svc)

    if retriever_override is not None:
        set_case_retriever(retriever_override)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with lifespan(
            app,
            engine_override=engine_override,
            case_service_override=case_service_override,
            retriever_override=retriever_override,
        ):
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
    app.include_router(evaluations_router)

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
