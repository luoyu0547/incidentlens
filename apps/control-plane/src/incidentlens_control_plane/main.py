"""HTTP entrypoint for the cloud incident control plane.

No route in this module contacts a remote server. Remote execution will be
wired only through the typed policy gate and provider adapters.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from incidentlens_control_plane.config import RuntimeSettings, build_runtime


@asynccontextmanager
async def _lifespan(app: FastAPI, settings: RuntimeSettings) -> AsyncIterator[None]:
    """Build services on startup and clear them on shutdown."""
    services = build_runtime(settings)
    app.state.runtime = services
    try:
        yield
    finally:
        app.state.runtime = None  # type: ignore[assignment]


def create_app(settings: RuntimeSettings | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    When *settings* is ``None`` a default configuration is used (suitable for
    Uvicorn ``--factory`` usage).
    """
    if settings is None:
        settings = RuntimeSettings.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with _lifespan(app, settings):
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

    return application


app = create_app()
