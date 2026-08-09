"""Shared fixtures for async API integration tests.

Provides:
  - FakeAsyncEngine: minimal engine satisfying InvestigationEngineProtocol
  - agent_api_client: httpx AsyncClient wired to create_app with the fake engine
  - export_client: httpx AsyncClient wired with export service and engine
"""

from __future__ import annotations

import httpx
import pytest
from incidentlens_contracts.models import InvestigationStatus
from incidentlens_control_plane.agent.state import InvestigationState
from incidentlens_control_plane.llm.config import RuntimeMode
from incidentlens_control_plane.main import create_app

# ---------------------------------------------------------------------------
# Fake engine
# ---------------------------------------------------------------------------


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

    async def load(self, incident_id: str) -> InvestigationState | None:
        """Load investigation state -- used by export endpoint."""
        if incident_id == self.state.incident_id:
            return self.state
        return None


# ---------------------------------------------------------------------------
# Agent API client (existing)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Investigation export client
# ---------------------------------------------------------------------------


@pytest.fixture
async def export_client(investigation_audit_store):
    """AsyncClient wired with export service for investigation export tests.

    The engine is pre-seeded with an investigation so the export endpoint
    can load a state for incident_id='inc-api'.
    """
    engine = FakeAsyncEngine(investigation_audit_store)
    # Seed the engine with an investigation so load() returns a state
    await engine.start({"service": "order-service", "symptom": "timeout"})

    app = create_app(engine_override=engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client
