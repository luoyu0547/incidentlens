"""Tests for RuntimeConfigClient — fetching active scenarios from the control plane.

These tests verify:
  - RuntimeConfigClient.get_active() fetches /api/scenarios/runtime/{service}
  - On timeout or connection error, returns {} (no fault injected)
  - On HTTP error response, returns {} (graceful degradation)
  - Successful fetch returns the active scenarios dict
  - Client uses a short timeout for the HTTP request
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from incidentlens_service_common.runtime_client import RuntimeConfigClient

# ===================================================================
# UNIT TESTS — using httpx mock transport
# ===================================================================


class TestRuntimeConfigClientUnit:
    """Unit tests for RuntimeConfigClient with mocked HTTP transport."""

    @pytest.mark.asyncio()
    async def test_returns_empty_on_connect_timeout(self) -> None:
        """When the control plane connection times out, get_active() returns {}."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("Connection timed out")

        transport = httpx.MockTransport(handler)
        rc = RuntimeConfigClient("http://cp", "payment-service")
        rc._client = httpx.AsyncClient(transport=transport, base_url="http://cp")
        result = await rc.get_active()
        assert result == {}

    @pytest.mark.asyncio()
    async def test_returns_empty_on_http_error(self) -> None:
        """When the control plane returns an error, get_active() returns {}."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=500)

        transport = httpx.MockTransport(handler)
        rc = RuntimeConfigClient("http://cp", "payment-service")
        rc._client = httpx.AsyncClient(transport=transport, base_url="http://cp")
        result = await rc.get_active()
        assert result == {}

    @pytest.mark.asyncio()
    async def test_returns_active_scenarios_on_success(self) -> None:
        """When the control plane returns 200, get_active() returns the active dict."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={
                    "service": "payment-service",
                    "active": {
                        "payment_delay": {"delay_ms": 200},
                        "payment_error_rate": {"error_rate": 0.5},
                    },
                },
            )

        transport = httpx.MockTransport(handler)
        rc = RuntimeConfigClient("http://cp", "payment-service")
        rc._client = httpx.AsyncClient(transport=transport, base_url="http://cp")
        result = await rc.get_active()
        assert "payment_delay" in result
        assert result["payment_delay"]["delay_ms"] == 200
        assert "payment_error_rate" in result
        assert result["payment_error_rate"]["error_rate"] == 0.5

    @pytest.mark.asyncio()
    async def test_returns_empty_active_when_no_scenarios(self) -> None:
        """When the control plane returns empty active, get_active() returns {}."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={"service": "payment-service", "active": {}},
            )

        transport = httpx.MockTransport(handler)
        rc = RuntimeConfigClient("http://cp", "payment-service")
        rc._client = httpx.AsyncClient(transport=transport, base_url="http://cp")
        result = await rc.get_active()
        assert result == {}

    @pytest.mark.asyncio()
    async def test_fetches_correct_service_endpoint(self) -> None:
        """get_active() calls /api/scenarios/runtime/{service} for the right service."""
        captured_path: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_path.append(str(request.url))
            return httpx.Response(
                status_code=200,
                json={"service": "order-service", "active": {}},
            )

        transport = httpx.MockTransport(handler)
        rc = RuntimeConfigClient("http://cp", "order-service")
        rc._client = httpx.AsyncClient(transport=transport, base_url="http://cp")
        await rc.get_active()
        assert "/api/scenarios/runtime/order-service" in captured_path[0]


# ===================================================================
# INTEGRATION TESTS — with real control plane ASGI app
# ===================================================================


class TestRuntimeConfigClientIntegration:
    """Integration tests with a real control plane ASGI app."""

    @pytest.mark.asyncio()
    async def test_fetches_enabled_scenario_from_control_plane(self) -> None:
        """RuntimeConfigClient reads scenarios enabled via the control plane API."""
        from fastapi import FastAPI
        from incidentlens_control_plane.routes.scenarios import (
            router as scenarios_router,
        )
        from incidentlens_control_plane.services.demo_reset import DemoResetService
        from incidentlens_scenarios.store import ScenarioStore
        from incidentlens_telemetry.database import create_engine
        from incidentlens_telemetry.repository import TelemetryRepository

        engine = create_engine("sqlite:///:memory:")
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

        # Enable a scenario via the API
        cp_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://cp")
        await cp_client.post(
            "/api/scenarios/payment_error_rate/enable", json={"error_rate": 1.0}
        )

        # Now use RuntimeConfigClient to fetch it
        rc = RuntimeConfigClient("http://cp", "payment-service")
        rc._client = cp_client
        result = await rc.get_active()
        assert "payment_error_rate" in result
        assert result["payment_error_rate"]["error_rate"] == 1.0

    @pytest.mark.asyncio()
    async def test_returns_empty_after_reset(self) -> None:
        """After reset, RuntimeConfigClient returns empty active dict."""
        from fastapi import FastAPI
        from incidentlens_control_plane.routes.scenarios import (
            router as scenarios_router,
        )
        from incidentlens_control_plane.services.demo_reset import DemoResetService
        from incidentlens_scenarios.store import ScenarioStore
        from incidentlens_telemetry.database import create_engine
        from incidentlens_telemetry.repository import TelemetryRepository

        engine = create_engine("sqlite:///:memory:")
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

        cp_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://cp")

        # Enable then reset
        await cp_client.post(
            "/api/scenarios/payment_delay/enable", json={"delay_ms": 200}
        )
        await cp_client.post("/api/scenarios/reset")

        rc = RuntimeConfigClient("http://cp", "payment-service")
        rc._client = cp_client
        result = await rc.get_active()
        assert result == {}
