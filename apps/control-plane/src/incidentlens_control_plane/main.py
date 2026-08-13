"""HTTP entrypoint for the cloud incident control plane.

No route in this module contacts a remote server. Remote execution will be
wired only through the typed policy gate and provider adapters.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.remote_ops.transport import RemoteTransportFactory
from incidentlens_control_plane.runtime import build_runtime

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
        # Then reconcile decided-but-unhandled approvals and scan the
        # investigations/checkpoints left over from a previous process, so a
        # restart never replays a dangerous in-flight operation.
        try:
            await services.recovery.startup()
        except Exception:
            logger.exception("failed to run investigation startup recovery")
        yield
    finally:
        # Orderly shutdown: stop accepting new investigations, request active
        # loops to checkpoint/cancel/drain and sweep unconfirmable dangerous
        # calls to UNCERTAIN, then close investigations/children, then log
        # subscriptions, and only then the host sessions.
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
    )

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
    from incidentlens_control_plane.web.routes import router as web_router

    application.include_router(approvals_router)
    application.include_router(changes_router)
    application.include_router(events_router)
    application.include_router(projects_router)
    application.include_router(remote_sessions_router)
    application.include_router(logs_router)
    application.include_router(evidence_router)
    application.include_router(incidents_router)
    application.include_router(investigations_router)
    application.include_router(web_router)

    _static_dir = Path(__file__).parent / "web" / "static"
    application.mount(
        "/static", StaticFiles(directory=str(_static_dir)), name="static"
    )

    return application


app = create_app()
