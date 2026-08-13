"""Web UI 页面路由。"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from incidentlens_control_plane.approvals.types import ApprovalStatus
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


@router.get("/web/approvals", response_class=HTMLResponse)
def approvals_list(request: Request) -> str:
    runtime = _get_runtime(request)
    pending = runtime.approvals.list(ApprovalStatus.PENDING)
    template = _env.get_template("approvals/list.html")
    return template.render(approvals=pending)


@router.post("/web/approvals/{approval_id}/approve")
async def approve_action(request: Request, approval_id: str) -> HTMLResponse:
    runtime = _get_runtime(request)
    await runtime.approvals.approve(approval_id)
    template = _env.get_template("approvals/_action.html")
    return HTMLResponse(
        template.render(approval_id=approval_id, status="approved"),
        headers={"HX-Trigger": "approval-updated"},
    )


@router.post("/web/approvals/{approval_id}/reject")
async def reject_action(request: Request, approval_id: str) -> HTMLResponse:
    runtime = _get_runtime(request)
    await runtime.approvals.reject(approval_id)
    template = _env.get_template("approvals/_action.html")
    return HTMLResponse(
        template.render(approval_id=approval_id, status="rejected"),
        headers={"HX-Trigger": "approval-updated"},
    )


@router.get("/web/logs/search", response_class=HTMLResponse)
def logs_search(request: Request) -> str:
    template = _env.get_template("logs/search.html")
    return template.render(results=[])


@router.get("/web/evidence/{evidence_ref_id}", response_class=HTMLResponse)
def evidence_detail(request: Request, evidence_ref_id: str) -> str:
    template = _env.get_template("evidence/detail.html")
    return template.render(evidence_id=evidence_ref_id, evidence=None)


@router.get("/web/reports/{investigation_id}", response_class=HTMLResponse)
def report_view(request: Request, investigation_id: str) -> HTMLResponse:
    runtime = _get_runtime(request)
    try:
        bundle = runtime.reports.generate(investigation_id)
        html_content = bundle.html_path.read_text(encoding="utf-8")
        return HTMLResponse(html_content)
    except Exception:
        template = _env.get_template("reports/render.html")
        return template.render(
            error="Report not available", investigation_id=investigation_id
        )


@router.get("/web/projects", response_class=HTMLResponse)
def projects_manage(request: Request) -> str:
    runtime = _get_runtime(request)
    projects = runtime.projects.list()
    template = _env.get_template("projects/manage.html")
    return template.render(projects=projects)


@router.get("/web/events/stream")
async def events_stream(request: Request) -> StreamingResponse:
    runtime = _get_runtime(request)
    broker = runtime.broker

    async def generate():
        async with broker.subscribe() as queue:
            while True:
                event = await queue.get()
                yield f"data: {event.model_dump_json()}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
