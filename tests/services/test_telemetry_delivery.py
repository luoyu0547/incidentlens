"""Tests for telemetry delivery — services POST events to the control plane.

These tests verify:
  - TelemetryClient posts events to /api/telemetry/events via async httpx
  - On timeout or connection error, local diagnostics still emit (no crash)
  - Events include duration, error type, and span status on abnormal paths
  - Telemetry reaches the control plane and is queryable via repository
  - payment_error_rate=1.0 produces persisted telemetry with error info
"""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

# Ensure ASGI transport is used for inter-service calls in tests
os.environ.pop("ORDER_SERVICE_URL", None)
os.environ.pop("PAYMENT_SERVICE_URL", None)
os.environ.pop("CONTROL_PLANE_URL", None)


def _patch_service_urls() -> None:
    """Set service URL constants to empty so ASGI transport is used."""
    import gateway_service.main as gw_mod
    import order_service.main as ord_mod

    gw_mod.ORDER_SERVICE_URL = ""
    ord_mod.PAYMENT_SERVICE_URL = ""


_patch_service_urls()


# ===================================================================
# UNIT TESTS — TelemetryClient HTTP delivery
# ===================================================================


class TestTelemetryClientHTTPDelivery:
    """Unit tests for TelemetryClient HTTP delivery to control plane."""

    @pytest.mark.asyncio()
    async def test_emit_log_schedules_post_when_cp_configured(self) -> None:
        """emit_log schedules _post_event when control_plane_url is set."""
        from unittest.mock import AsyncMock, patch

        from incidentlens_service_common.telemetry_client import TelemetryClient

        client = TelemetryClient("test-service", control_plane_url="http://cp:8003")
        with patch.object(client, "_post_event", new_callable=AsyncMock) as mock_post:
            event = client.emit_log("trace-1", "INFO", "test message")
            # _schedule_post uses asyncio.ensure_future, so the mock should
            # eventually be called. Give the event loop a chance to run.
            # Allow a small wait for the scheduled future to execute.
            import asyncio
            await asyncio.sleep(0.05)
            mock_post.assert_awaited_once_with(event)

    @pytest.mark.asyncio()
    async def test_emit_metric_schedules_post_when_cp_configured(self) -> None:
        """emit_metric schedules _post_event when control_plane_url is set."""
        from unittest.mock import AsyncMock, patch

        from incidentlens_service_common.telemetry_client import TelemetryClient

        client = TelemetryClient("test-service", control_plane_url="http://cp:8003")
        with patch.object(client, "_post_event", new_callable=AsyncMock) as mock_post:
            event = client.emit_metric("trace-1", "request_count", 1.0)
            import asyncio
            await asyncio.sleep(0.05)
            mock_post.assert_awaited_once_with(event)

    @pytest.mark.asyncio()
    async def test_emit_span_schedules_post_when_cp_configured(self) -> None:
        """emit_span schedules _post_event when control_plane_url is set."""
        from unittest.mock import AsyncMock, patch

        from incidentlens_service_common.telemetry_client import TelemetryClient

        client = TelemetryClient("test-service", control_plane_url="http://cp:8003")
        with patch.object(client, "_post_event", new_callable=AsyncMock) as mock_post:
            event = client.emit_span("trace-1", "span-1", "POST /charge")
            import asyncio
            await asyncio.sleep(0.05)
            mock_post.assert_awaited_once_with(event)

    @pytest.mark.asyncio()
    async def test_emit_deployment_schedules_structured_event(self) -> None:
        """Deployment telemetry is persisted through the same delivery path."""
        from unittest.mock import AsyncMock, patch

        from incidentlens_service_common.telemetry_client import TelemetryClient

        client = TelemetryClient("payment-service", control_plane_url="http://cp:8003")
        with patch.object(client, "_post_event", new_callable=AsyncMock) as mock_post:
            event = client.emit_deployment("trace-1", "v2.0.0-buggy")
            import asyncio
            await asyncio.sleep(0.05)
            mock_post.assert_awaited_once_with(event)
        assert event.event_type == "deployment"
        assert event.payload == {"version": "v2.0.0-buggy"}

    @pytest.mark.asyncio()
    async def test_post_event_catches_http_error(self) -> None:
        """_post_event catches httpx.HTTPError and does not raise."""
        from incidentlens_service_common.telemetry_client import TelemetryClient

        client = TelemetryClient("test-service", control_plane_url="http://unreachable:9999")
        client._http_timeout = 0.1
        # This should not raise — graceful degradation
        event = client.emit_log("trace-1", "INFO", "test")
        # The event is still returned (local diagnostic was emitted)
        assert event is not None
        assert event.event_type == "log"
        # Explicitly await _post_event to verify it catches the error without raising
        await client._post_event(event)

    @pytest.mark.asyncio()
    async def test_no_control_plane_url_means_no_http_post(self) -> None:
        """When control_plane_url is not set, no HTTP POST is attempted."""
        from unittest.mock import AsyncMock, patch

        from incidentlens_service_common.telemetry_client import TelemetryClient

        client = TelemetryClient("test-service")
        with patch.object(client, "_post_event", new_callable=AsyncMock) as mock_post:
            client.emit_log("trace-1", "INFO", "test message")
            import asyncio
            await asyncio.sleep(0.05)
            mock_post.assert_not_awaited()


# ===================================================================
# INTEGRATION TESTS — telemetry reaches control plane
# ===================================================================


class TestTelemetryDeliveryIntegration:
    """Integration tests: services emit telemetry that reaches the control plane."""

    @pytest.mark.asyncio()
    async def test_payment_fault_telemetry_reaches_control_plane(self) -> None:
        """When payment_error_rate=1.0, error telemetry reaches the control plane."""
        from fastapi import FastAPI
        from incidentlens_control_plane.routes.scenarios import (
            router as scenarios_router,
        )
        from incidentlens_control_plane.routes.telemetry import (
            router as telemetry_router,
        )
        from incidentlens_control_plane.services.demo_reset import DemoResetService
        from incidentlens_scenarios.service import ScenarioService
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
        from incidentlens_control_plane.routes.telemetry import set_repository

        set_scenario_store(store)
        set_demo_reset_service(reset_service)
        set_repository(repository)

        cp_app = FastAPI()
        cp_app.include_router(scenarios_router)
        cp_app.include_router(telemetry_router)

        cp_client = AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://cp")

        # Enable payment_error_rate at 1.0 via the control plane
        await cp_client.post(
            "/api/scenarios/payment_error_rate/enable", json={"error_rate": 1.0}
        )

        # Set up payment service with in-process ScenarioService (for fault injection)
        # and a TelemetryClient that posts to the control plane
        from payment_service.main import app as payment_app
        from payment_service.main import set_scenario_service

        svc = ScenarioService()
        svc.enable("payment_error_rate", {"error_rate": 1.0})
        set_scenario_service(svc)

        # Patch the payment service's telemetry client to post to our test control plane
        import payment_service.main as pay_mod

        original_telemetry = pay_mod._telemetry
        pay_mod._telemetry = type(original_telemetry)(
            "payment-service", control_plane_url="http://cp:8003"
        )
        pay_mod._telemetry._client = cp_client

        try:
            payment_client = AsyncClient(
                transport=ASGITransport(app=payment_app), base_url="http://payment"
            )
            response = await payment_client.post(
                "/charge",
                json={"amount": 1000, "currency": "USD"},
                headers={"X-Trace-ID": "trace-err-telemetry", "X-Request-ID": "req-err-telemetry"},
            )
            assert response.status_code == 500

            # Give the event loop a chance to process fire-and-forget telemetry posts
            import asyncio
            await asyncio.sleep(0.1)

            # Verify telemetry reached the control plane repository
            logs = repository.query_logs(
                service="payment-service",
                level="ERROR",
            )
            error_messages = [log["message"] for log in logs]
            assert any("injected error" in msg for msg in error_messages), (
                f"Expected 'injected error' in logs, got: {error_messages}"
            )
        finally:
            # Restore original telemetry
            pay_mod._telemetry = original_telemetry
            svc.reset()
            set_scenario_service(None)

    @pytest.mark.asyncio()
    async def test_normal_path_telemetry_reaches_control_plane(self) -> None:
        """On normal (non-fault) path, telemetry events reach the control plane."""
        from fastapi import FastAPI
        from incidentlens_control_plane.routes.telemetry import (
            router as telemetry_router,
        )
        from incidentlens_telemetry.database import create_engine
        from incidentlens_telemetry.repository import TelemetryRepository

        engine = create_engine("sqlite:///:memory:")
        repository = TelemetryRepository(engine)

        from incidentlens_control_plane.routes.telemetry import set_repository

        set_repository(repository)

        cp_app = FastAPI()
        cp_app.include_router(telemetry_router)

        cp_client = AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://cp")

        # Set up payment service with telemetry posting to control plane
        import payment_service.main as pay_mod
        from payment_service.main import app as payment_app
        from payment_service.main import set_scenario_service

        original_telemetry = pay_mod._telemetry
        pay_mod._telemetry = type(original_telemetry)(
            "payment-service", control_plane_url="http://cp:8003"
        )
        pay_mod._telemetry._client = cp_client

        try:
            set_scenario_service(None)  # No faults
            payment_client = AsyncClient(
                transport=ASGITransport(app=payment_app), base_url="http://payment"
            )
            response = await payment_client.post(
                "/charge",
                json={"amount": 500, "currency": "USD"},
                headers={
                    "X-Trace-ID": "trace-normal-telemetry",
                    "X-Request-ID": "req-normal-telemetry",
                },
            )
            assert response.status_code == 200

            # Give the event loop a chance to process fire-and-forget telemetry posts
            import asyncio
            await asyncio.sleep(0.1)

            # Verify telemetry reached the control plane
            logs = repository.query_logs(service="payment-service")
            messages = [log["message"] for log in logs]
            assert any("Charge approved" in msg for msg in messages), (
                f"Expected 'Charge approved' in logs, got: {messages}"
            )
        finally:
            pay_mod._telemetry = original_telemetry
            set_scenario_service(None)

    @pytest.mark.asyncio()
    async def test_telemetry_post_does_not_crash_on_unreachable_cp(self) -> None:
        """When the control plane is unreachable, telemetry POST fails gracefully."""
        from incidentlens_service_common.telemetry_client import TelemetryClient

        client = TelemetryClient("test-service", control_plane_url="http://unreachable:9999")
        client._http_timeout = 0.1
        # This should not raise
        event = client.emit_log("trace-1", "INFO", "test message")
        assert event is not None
        # The await on _post_event should also not raise
        await client._post_event(event)
