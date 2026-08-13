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


@router.get("/web/investigations/{investigation_id}", response_class=HTMLResponse)
async def investigation_detail(request: Request, investigation_id: str) -> str:
    runtime = _get_runtime(request)
    try:
        investigation = runtime.investigations.get_investigation(investigation_id)
    except Exception:
        investigation = None
    runs = []
    hypotheses = []
    conclusions = []
    tool_calls = []
    if investigation:
        runs = list(runtime.investigations.list_runs(investigation_id=investigation_id))
        hypotheses = list(runtime.investigations.list_hypotheses(investigation_id=investigation_id))
        conclusions = list(
            runtime.investigations.list_conclusions(investigation_id=investigation_id)
        )
        for run in runs:
            tool_calls.extend(
                runtime.investigation_store.list_tool_calls(agent_run_id=run.agent_run_id)
            )
    template = _env.get_template("investigations/detail.html")
    return template.render(
        investigation=investigation,
        investigation_id=investigation_id,
        runs=runs,
        hypotheses=hypotheses,
        conclusions=conclusions,
        tool_calls=tool_calls,
    )
