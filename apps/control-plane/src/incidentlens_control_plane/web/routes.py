"""Web UI 页面路由。"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from incidentlens_control_plane.runtime import RuntimeServices
from incidentlens_control_plane.web.dependencies import get_jinja_env

router = APIRouter(tags=["web"])

_env = get_jinja_env()


def _get_runtime(request: Request) -> RuntimeServices:
    return cast(RuntimeServices, request.app.state.runtime)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> str:
    runtime = _get_runtime(request)
    investigations = runtime.investigations.list_investigations()
    template = _env.get_template("dashboard.html")
    return template.render(investigations=investigations)


@router.get("/web/investigations", response_class=HTMLResponse)
async def investigations_list(request: Request) -> str:
    runtime = _get_runtime(request)
    investigations = runtime.investigations.list_investigations()
    template = _env.get_template("investigations/list.html")
    return template.render(investigations=investigations)
