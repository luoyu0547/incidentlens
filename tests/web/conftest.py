"""Shared fixtures for async API integration tests.

Provides:
  - FakeAsyncEngine: minimal engine satisfying InvestigationEngineProtocol
  - agent_api_client: httpx AsyncClient wired to create_app with the fake engine
  - case_api_client: httpx AsyncClient wired with case governance services
  - export_client: httpx AsyncClient wired with export service and engine
"""

from __future__ import annotations

import httpx
import pytest
from incidentlens_contracts.models import InvestigationStatus
from incidentlens_control_plane.agent.state import InvestigationState
from incidentlens_control_plane.llm.config import RuntimeMode
from incidentlens_control_plane.main import create_app
from incidentlens_control_plane.memory.domain import (
    CaseSearchHit,
    CaseSearchQuery,
)
from incidentlens_control_plane.memory.repository import CaseRepository
from incidentlens_control_plane.memory.service import CaseService
from incidentlens_telemetry.database import create_engine

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
# Fake hybrid retriever for case search
# ---------------------------------------------------------------------------


class FakeHybridRetriever:
    """Minimal retriever that returns empty results for testing the route layer."""

    last_degradation_reason: str | None = None

    def search(self, query: CaseSearchQuery) -> list[CaseSearchHit]:
        """Return empty results -- route layer tests verify structure, not ranking."""
        return []


# ---------------------------------------------------------------------------
# Shared in-memory database for case services
# ---------------------------------------------------------------------------


@pytest.fixture
def case_db_engine():
    """Create an in-memory SQLite engine for case tests."""
    return create_engine("sqlite:///:memory:")


@pytest.fixture
def case_repository(case_db_engine):
    """Create a CaseRepository backed by an in-memory database."""
    return CaseRepository(case_db_engine)


@pytest.fixture
def case_service(case_repository):
    """Create a CaseService for the case governance tests."""
    return CaseService(case_repository)


@pytest.fixture
def hybrid_retriever():
    """Create a FakeHybridRetriever for route tests."""
    return FakeHybridRetriever()


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
# Case governance API client
# ---------------------------------------------------------------------------


@pytest.fixture
async def case_api_client(case_service, hybrid_retriever):
    """AsyncClient wired with case service and hybrid retriever for governance tests."""
    app = create_app(
        engine_override=None,
        case_service_override=case_service,
        retriever_override=hybrid_retriever,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Investigation export client
# ---------------------------------------------------------------------------


@pytest.fixture
async def export_client(investigation_audit_store, case_service):
    """AsyncClient wired with export service for investigation export tests.

    The engine is pre-seeded with an investigation so the export endpoint
    can load a state for incident_id='inc-api'.
    """
    engine = FakeAsyncEngine(investigation_audit_store)
    # Seed the engine with an investigation so load() returns a state
    await engine.start({"service": "order-service", "symptom": "timeout"})

    app = create_app(
        engine_override=engine,
        case_service_override=case_service,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client
