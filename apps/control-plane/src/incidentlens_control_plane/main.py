"""HTTP entrypoint for the cloud incident control plane.

No route in this module contacts a remote server. Remote execution will be
wired only through the typed policy gate and provider adapters.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from incidentlens_control_plane.api.errors import install_error_handlers
from incidentlens_control_plane.api.request_id import RequestIdMiddleware
from incidentlens_control_plane.api.router import router as v1_router
from incidentlens_control_plane.api.routes import agent_sessions as agent_sessions_routes
from incidentlens_control_plane.api.routes import approvals as approvals_v1_routes
from incidentlens_control_plane.api.routes import changes as changes_routes
from incidentlens_control_plane.api.routes import events as v1_events_routes
from incidentlens_control_plane.api.routes import evidence as evidence_v1_routes
from incidentlens_control_plane.api.routes import (
    investigation_summaries as investigation_summary_routes,
)
from incidentlens_control_plane.api.routes import issues as issue_routes
from incidentlens_control_plane.api.routes import operations as operations_routes
from incidentlens_control_plane.api.routes import overview as overview_routes
from incidentlens_control_plane.api.routes import services as services_routes
from incidentlens_control_plane.api.routes import targets as targets_routes
from incidentlens_control_plane.api.routes.auth import (
    auth_router,
    session_router,
)
from incidentlens_control_plane.config import (
    DEFAULT_SESSION_SIGNING_KEY,
    RuntimeSettings,
)
from incidentlens_control_plane.remote_ops.transport import RemoteTransportFactory
from incidentlens_control_plane.runtime import build_runtime
from incidentlens_control_plane.web_assets import mount_web_assets

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(
    app: FastAPI,
    settings: RuntimeSettings,
    *,
    transport_factory: RemoteTransportFactory | None = None,
) -> AsyncIterator[None]:
    """Build services on startup and close sessions on shutdown."""
    services = build_runtime(settings, transport_factory=transport_factory)
    app.state.runtime = services
    try:
        # Restore active opt-in log subscriptions before any request is served.
        # A single failing subscription must not prevent startup: the manager
        # isolates per-subscription reader errors, and this guard covers any
        # unexpected store/setup failure.
        try:
            await services.subscriptions.start_active_opt_in()
        except Exception:
            logger.exception("failed to restore active log subscriptions")
        if settings.legacy_api_enabled:
            logger.warning(
                "legacy /api/* routes are enabled (INCIDENTLENS_LEGACY_API_ENABLED=true); "
                "migrate clients to the authenticated /api/v1 surface before disabling"
            )
        if settings.session_signing_key.get_secret_value() == DEFAULT_SESSION_SIGNING_KEY:
            logger.warning(
                "session signing key is the documented non-production dev default; "
                "set INCIDENTLENS_SESSION_SIGNING_KEY before deploying"
            )
        # Then reconcile decided-but-unhandled approvals and scan the
        # investigations/checkpoints left over from a previous process, so a
        # restart never replays a dangerous in-flight operation.
        try:
            await services.recovery.startup()
        except Exception:
            logger.exception("failed to run investigation startup recovery")
        # Then classify leftover durable operations (a dangerous RUNNING
        # rollback becomes UNCERTAIN, never replayed) and start the worker pool
        # that claims queued operations.
        try:
            await services.dispatcher.start()
        except Exception:
            logger.exception("failed to start operation dispatcher")
        yield
    finally:
        # Orderly shutdown: stop the operation dispatcher first so no durable
        # operation is running while the investigation stack and host sessions
        # are being torn down, then stop accepting new investigations, request
        # active loops to checkpoint/cancel/drain and sweep unconfirmable
        # dangerous calls to UNCERTAIN, then close investigations/children, then
        # log subscriptions, and only then the host sessions.
        try:
            await services.dispatcher.stop(grace_seconds=settings.shutdown_grace_seconds)
        except Exception:
            logger.exception("failed to stop operation dispatcher")
        try:
            await services.recovery.shutdown()
        except Exception:
            logger.exception("failed to shut down investigations")
        try:
            await services.subscriptions.close_all()
        except Exception:
            logger.exception("failed to close log subscriptions")
        try:
            await services.sessions.close_all()
        except Exception:
            logger.exception("failed to close host sessions")
        app.state.runtime = None  # type: ignore[assignment]


def create_app(
    settings: RuntimeSettings | None = None,
    *,
    transport_factory: RemoteTransportFactory | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    When *settings* is ``None`` a default configuration is used (suitable for
    Uvicorn ``--factory`` usage).
    """
    if settings is None:
        settings = RuntimeSettings.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with _lifespan(app, settings, transport_factory=transport_factory):
            yield

    application = FastAPI(
        title="IncidentLens",
        version="0.2.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.expose_api_docs else None,
        redoc_url="/redoc" if settings.expose_api_docs else None,
        openapi_url="/openapi.json" if settings.expose_api_docs else None,
    )

    # Per-request request IDs and the versioned error envelope.  Handlers are
    # registered app-wide but scoped to /api/v1 at call time; legacy /api/*
    # error bodies are delegated to FastAPI's defaults byte-for-byte.
    application.add_middleware(RequestIdMiddleware)
    if settings.trusted_hosts:
        application.add_middleware(
            TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts)
        )
    install_error_handlers(application)

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Return process health without claiming any target is connected."""
        return {"status": "ok", "remote_execution": "not_configured"}

    # Include routers
    from incidentlens_control_plane.routes.approvals import router as approvals_router
    from incidentlens_control_plane.routes.changes import router as changes_router
    from incidentlens_control_plane.routes.events import router as events_router
    from incidentlens_control_plane.routes.evidence import (
        incidents_router,
    )
    from incidentlens_control_plane.routes.evidence import (
        router as evidence_router,
    )
    from incidentlens_control_plane.routes.investigations import (
        router as investigations_router,
    )
    from incidentlens_control_plane.routes.logs import router as logs_router
    from incidentlens_control_plane.routes.projects import router as projects_router
    from incidentlens_control_plane.routes.remote_sessions import (
        router as remote_sessions_router,
    )

    application.include_router(v1_router)
    application.include_router(auth_router)
    application.include_router(session_router)
    application.include_router(targets_routes.router)
    application.include_router(agent_sessions_routes.router)
    application.include_router(approvals_v1_routes.router)
    application.include_router(v1_events_routes.router)
    application.include_router(operations_routes.router)
    application.include_router(changes_routes.router)
    application.include_router(overview_routes.router)
    application.include_router(services_routes.router)
    application.include_router(issue_routes.router)
    application.include_router(investigation_summary_routes.router)
    application.include_router(evidence_v1_routes.router)

    from incidentlens_control_plane.api.routes.service_logs import (
        router as service_logs_router,
    )
    from incidentlens_control_plane.api.routes.workspace_events import (
        router as workspace_events_router,
    )
    from incidentlens_control_plane.api.ws.cli_events import router as cli_ws_router
    from incidentlens_control_plane.api.ws.logs import router as logs_ws_router

    application.include_router(cli_ws_router)
    application.include_router(logs_ws_router)
    application.include_router(service_logs_router)
    application.include_router(workspace_events_router)

    if settings.legacy_api_enabled:
        application.include_router(approvals_router)
        application.include_router(changes_router)
        application.include_router(events_router)
        application.include_router(projects_router)
        application.include_router(remote_sessions_router)
        application.include_router(logs_router)
        application.include_router(evidence_router)
        application.include_router(incidents_router)
        application.include_router(investigations_router)

    if settings.web_root is not None:
        mount_web_assets(application, web_root=settings.web_root)

    return application


app = create_app()
