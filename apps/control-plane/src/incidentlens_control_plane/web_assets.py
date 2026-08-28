"""Serve the built Vite single-page application without masking API errors."""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.responses import Response
from starlette.staticfiles import StaticFiles


class ImmutableStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

_ALLOWED_SPA_PREFIXES = ("services/", "issues/", "investigations/")
_RESERVED_PREFIXES = (
    "api",
    "events",
    "ws",
    "healthz",
    "assets",
    "docs",
    "redoc",
    "openapi.json",
)


def _is_spa_path(path: str) -> bool:
    """Return whether a browser history path is one of the product routes."""
    normalized = path.strip("/")
    if not normalized or normalized in {"issues"}:
        return True
    if any(
        normalized == prefix or normalized.startswith(f"{prefix}/")
        for prefix in _RESERVED_PREFIXES
    ):
        return False
    if "." in normalized.rsplit("/", 1)[-1]:
        return False
    return normalized.startswith(_ALLOWED_SPA_PREFIXES)


def mount_web_assets(app: FastAPI, *, web_root: Path) -> None:
    """Mount hashed assets and an allow-listed SPA fallback."""
    root = web_root.resolve()
    assets = root / "assets"
    index = root / "index.html"

    if assets.is_dir():
        app.mount("/assets", ImmutableStaticFiles(directory=assets), name="web-assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa_fallback(request: Request, path: str):
        if not _is_spa_path(path) or request.method != "GET":
            raise HTTPException(status_code=404, detail="Not Found")
        if not index.is_file():
            raise HTTPException(status_code=503, detail="Web assets are not built")
        response = FileResponse(index, media_type="text/html")
        response.headers["Cache-Control"] = "no-cache"
        return response
