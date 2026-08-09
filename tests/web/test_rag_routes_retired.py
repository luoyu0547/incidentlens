"""Tests that legacy RAG routes and case payloads are retired.

Verifies:
  - /api/cases* routes return 404 (not registered)
  - Investigation response no longer exposes case_id or case_status
  - Export payload contains no case or case_usage keys
"""

from __future__ import annotations

import httpx
import pytest
from incidentlens_contracts.models import InvestigationStatus
from incidentlens_control_plane.agent.state import InvestigationState
from incidentlens_control_plane.llm.config import RuntimeMode
from incidentlens_control_plane.main import create_app


# ---------------------------------------------------------------------------
# Minimal fake engine for route tests
# ---------------------------------------------------------------------------


class _MinimalEngine:
    """Minimal engine that returns a state without case fields."""

    mode = RuntimeMode.LLM_AGENT

    def __init__(self, audit_store=None) -> None:
        self.audit_store = audit_store
        self.state = InvestigationState(
            incident_id="inc-retire",
            status=InvestigationStatus.INVESTIGATING,
            alert={"service": "order-service"},
            phase="agent_loop",
        )

    async def start(self, alert):
        self.state.alert = alert
        return self.state

    async def run_round(self, incident_id):
        return self.state

    async def resume(self, incident_id):
        return self.state

    async def load(self, incident_id: str):
        if incident_id == self.state.incident_id:
            return self.state
        return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture()
async def retire_client(investigation_audit_store):
    """App wired without case services or retriever."""
    engine = _MinimalEngine(audit_store=investigation_audit_store)
    await engine.start({"service": "order-service"})
    app = create_app(engine_override=engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


async def test_case_routes_are_not_registered(retire_client) -> None:
    """GET /api/cases/search must return 404 when case routes are retired."""
    response = await retire_client.get("/api/cases/search", params={"q": "timeout"})
    assert response.status_code == 404


async def test_case_post_route_is_not_registered(retire_client) -> None:
    """POST /api/cases must return 404 or 405 when case routes are retired."""
    response = await retire_client.post(
        "/api/cases",
        json={
            "symptom": "test",
            "affected_services": ["svc"],
            "actor": "tester",
        },
    )
    # 404 = not found (no route), 405 = method not allowed (static mount caught it)
    assert response.status_code in (404, 405)


async def test_investigation_response_has_no_case_fields(retire_client) -> None:
    """Investigation state response must not expose case_id or case_status."""
    response = await retire_client.post(
        "/api/investigations/start",
        json={"service": "order-service"},
    )
    assert response.status_code == 201
    body = response.json()
    assert "case_id" not in body
    assert "case_status" not in body


async def test_export_contains_no_case_payload(retire_client) -> None:
    """Export must not include case or case_usage keys."""
    response = await retire_client.get(
        "/api/investigations/inc-retire/export",
    )
    assert response.status_code == 200
    body = response.json()
    assert "case" not in body
    assert "case_usage" not in body
