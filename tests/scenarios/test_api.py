"""Tests for scenario API routes in the control plane.

These tests verify:
  - GET /api/scenarios returns list of all scenario definitions
  - POST /api/scenarios/{name}/enable activates a scenario
  - POST /api/scenarios/{name}/disable deactivates a scenario
  - POST /api/scenarios/reset clears all scenarios and demo data
  - GET /api/scenarios/runtime/{service} returns active scenarios for a service
  - root_cause_label is never exposed in any API response
  - Unknown scenarios return 404
  - Invalid parameter ranges return 422
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine

from incidentlens_telemetry.database import create_engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Engine:
    """Create an in-memory SQLite engine with all tables."""
    return create_engine("sqlite:///:memory:")


@pytest.fixture()
async def client(engine: Engine) -> AsyncClient:
    """Create an AsyncClient wired to a test control plane app."""
    from fastapi import FastAPI

    from incidentlens_control_plane.routes.scenarios import (
        router as scenarios_router,
    )
    from incidentlens_control_plane.services.demo_reset import DemoResetService
    from incidentlens_scenarios.store import ScenarioStore
    from incidentlens_telemetry.repository import TelemetryRepository

    store = ScenarioStore(engine)
    repository = TelemetryRepository(engine)
    reset_service = DemoResetService(repository, store)

    from incidentlens_control_plane.routes.scenarios import (
        set_demo_reset_service,
        set_scenario_store,
    )

    set_scenario_store(store)
    set_demo_reset_service(reset_service)

    app = FastAPI()
    app.include_router(scenarios_router)

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ===================================================================
# API ROUTE TESTS
# ===================================================================


class TestScenarioAPI:
    """Tests for scenario API routes."""

    @pytest.mark.asyncio()
    async def test_list_scenarios(self, client: AsyncClient) -> None:
        """GET /api/scenarios returns all scenario definitions."""
        response = await client.get("/api/scenarios")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 5  # 5 scenario definitions
        # Each entry should have name and target_service but NOT root_cause_label
        for scenario in data:
            assert "name" in scenario
            assert "target_service" in scenario
            assert "root_cause_label" not in scenario

    @pytest.mark.asyncio()
    async def test_enable_scenario(self, client: AsyncClient) -> None:
        """POST /api/scenarios/{name}/enable activates a scenario."""
        response = await client.post(
            "/api/scenarios/payment_delay/enable", json={"delay_ms": 250}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "payment_delay"
        assert data["active"] is True
        assert "root_cause_label" not in response.text

    @pytest.mark.asyncio()
    async def test_enable_and_reset_are_publicly_observable(
        self, client: AsyncClient
    ) -> None:
        """Enable and reset are publicly observable via runtime endpoint."""
        enabled = await client.post(
            "/api/scenarios/payment_error_rate/enable", json={"error_rate": 1.0}
        )
        assert enabled.status_code == 200
        assert "root_cause_label" not in enabled.text

        runtime = await client.get("/api/scenarios/runtime/payment-service")
        assert runtime.status_code == 200
        runtime_data = runtime.json()
        assert "payment_error_rate" in runtime_data["active"]

        reset = await client.post("/api/scenarios/reset")
        assert reset.status_code == 200

        # After reset, runtime should be empty
        runtime_after = await client.get("/api/scenarios/runtime/payment-service")
        assert runtime_after.json()["active"] == {}

    @pytest.mark.asyncio()
    async def test_disable_scenario(self, client: AsyncClient) -> None:
        """POST /api/scenarios/{name}/disable deactivates a scenario."""
        await client.post(
            "/api/scenarios/payment_delay/enable", json={"delay_ms": 250}
        )
        response = await client.post("/api/scenarios/payment_delay/disable")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "payment_delay"
        assert data["active"] is False

    @pytest.mark.asyncio()
    async def test_runtime_for_service(self, client: AsyncClient) -> None:
        """GET /api/scenarios/runtime/{service} returns active scenarios for service."""
        await client.post(
            "/api/scenarios/payment_delay/enable", json={"delay_ms": 300}
        )
        await client.post(
            "/api/scenarios/db_pool_exhaustion/enable", json={"pool_size": 2}
        )

        # payment-service should see payment_delay only
        response = await client.get("/api/scenarios/runtime/payment-service")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "payment-service"
        assert "payment_delay" in data["active"]
        assert "db_pool_exhaustion" not in data["active"]
        assert data["active"]["payment_delay"]["delay_ms"] == 300

        # order-service should see db_pool_exhaustion only
        response = await client.get("/api/scenarios/runtime/order-service")
        data = response.json()
        assert "db_pool_exhaustion" in data["active"]
        assert "payment_delay" not in data["active"]

    @pytest.mark.asyncio()
    async def test_runtime_never_exposes_root_cause_label(
        self, client: AsyncClient
    ) -> None:
        """Runtime endpoint must never expose root_cause_label."""
        await client.post(
            "/api/scenarios/payment_delay/enable", json={"delay_ms": 250}
        )
        response = await client.get("/api/scenarios/runtime/payment-service")
        assert "root_cause_label" not in response.text

    @pytest.mark.asyncio()
    async def test_enable_unknown_scenario_returns_404(
        self, client: AsyncClient
    ) -> None:
        """Enabling an unknown scenario returns 404."""
        response = await client.post("/api/scenarios/nonexistent/enable", json={})
        assert response.status_code == 404

    @pytest.mark.asyncio()
    async def test_disable_unknown_scenario_returns_404(
        self, client: AsyncClient
    ) -> None:
        """Disabling an unknown scenario returns 404."""
        response = await client.post("/api/scenarios/nonexistent/disable")
        assert response.status_code == 404

    @pytest.mark.asyncio()
    async def test_enable_invalid_params_returns_422(
        self, client: AsyncClient
    ) -> None:
        """Enabling a scenario with invalid parameter ranges returns 422."""
        response = await client.post(
            "/api/scenarios/payment_error_rate/enable", json={"error_rate": 1.5}
        )
        assert response.status_code == 422

    @pytest.mark.asyncio()
    async def test_reset_clears_all_demo_data(self, client: AsyncClient) -> None:
        """POST /api/scenarios/reset clears scenarios and demo data."""
        # Enable a scenario
        await client.post(
            "/api/scenarios/payment_delay/enable", json={"delay_ms": 250}
        )
        # Reset
        response = await client.post("/api/scenarios/reset")
        assert response.status_code == 200
        assert response.json()["status"] == "reset"

        # Verify scenarios are cleared
        runtime = await client.get("/api/scenarios/runtime/payment-service")
        assert runtime.json()["active"] == {}

    @pytest.mark.asyncio()
    async def test_enable_response_excludes_root_cause_label(
        self, client: AsyncClient
    ) -> None:
        """Enable response must not contain root_cause_label."""
        response = await client.post(
            "/api/scenarios/payment_delay/enable", json={"delay_ms": 250}
        )
        assert response.status_code == 200
        data = response.json()
        assert "root_cause_label" not in data
        assert "root_cause" not in str(data).lower()

    @pytest.mark.asyncio()
    async def test_list_scenarios_excludes_root_cause_label(
        self, client: AsyncClient
    ) -> None:
        """List scenarios response must not contain root_cause_label."""
        response = await client.get("/api/scenarios")
        assert "root_cause_label" not in response.text
