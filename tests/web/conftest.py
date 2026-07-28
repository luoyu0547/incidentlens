"""Shared fixtures for async API integration tests.

Provides:
  - FakeAsyncEngine: minimal engine satisfying InvestigationEngineProtocol
  - agent_api_client: httpx AsyncClient wired to create_app with the fake engine
"""

from __future__ import annotations

import httpx
import pytest
from incidentlens_contracts.models import InvestigationStatus

from incidentlens_control_plane.agent.state import InvestigationState
from incidentlens_control_plane.llm.config import RuntimeMode
from incidentlens_control_plane.main import create_app


class FakeAsyncEngine:
    """Minimal async engine for testing the API layer."""

    mode = RuntimeMode.LLM_AGENT

    def __init__(self, audit_store) -> None:
        self.audit_store = audit_store
        self.state = InvestigationState(
            incident_id="inc-api",
            status=InvestigationStatus.INVESTIGATING,
            alert={"service": "order-service"},
            phase="agent_loop",
            model_profile="deepseek",
            last_checkpoint_id="checkpoint-1",
        )

    async def start(self, alert):
        self.state.alert = alert
        return self.state

    async def run_round(self, incident_id):
        return self.state

    async def resume(self, incident_id):
        return self.state


@pytest.fixture
def fake_agent_engine(investigation_audit_store) -> FakeAsyncEngine:
    return FakeAsyncEngine(investigation_audit_store)


@pytest.fixture
async def agent_api_client(fake_agent_engine):
    app = create_app(engine_override=fake_agent_engine)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client
