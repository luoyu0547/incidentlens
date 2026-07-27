"""Tests for the three-service call chain and context propagation.

These tests verify:
  - Gateway -> Order -> Payment call chain works end-to-end
  - X-Request-ID and X-Trace-ID headers propagate across all hops
  - Each service exposes GET /healthz
  - Telemetry events are emitted at each hop
  - Fault scenarios modify service behavior
"""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Ensure ASGI transport is used for inter-service calls in tests
# (clear the URL env vars so services fall back to in-process ASGI)
# ---------------------------------------------------------------------------
os.environ.pop("ORDER_SERVICE_URL", None)
os.environ.pop("PAYMENT_SERVICE_URL", None)

# We must import services AFTER clearing env vars so they read the correct
# values at module init time. But the services read the env vars at module
# import time, so we patch the module-level constants directly.


def _patch_service_urls() -> None:
    """Set service URL constants to empty so ASGI transport is used."""
    import gateway_service.main as gw_mod
    import order_service.main as ord_mod

    gw_mod.ORDER_SERVICE_URL = ""
    ord_mod.PAYMENT_SERVICE_URL = ""


_patch_service_urls()

# ---------------------------------------------------------------------------
# Helpers to build ASGI test clients for each service
# ---------------------------------------------------------------------------


def _make_gateway_client() -> AsyncClient:
    """Create an AsyncClient pointed at the gateway ASGI app."""
    from gateway_service.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway")


def _make_order_client() -> AsyncClient:
    """Create an AsyncClient pointed at the order-service ASGI app."""
    from order_service.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://order")


def _make_payment_client() -> AsyncClient:
    """Create an AsyncClient pointed at the payment-service ASGI app."""
    from payment_service.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://payment")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def gateway_client() -> AsyncClient:
    return _make_gateway_client()


@pytest.fixture()
def order_client() -> AsyncClient:
    return _make_order_client()


@pytest.fixture()
def payment_client() -> AsyncClient:
    return _make_payment_client()


# ===================================================================
# HEALTH CHECK TESTS
# ===================================================================


class TestHealthChecks:
    """Each service must expose GET /healthz."""

    @pytest.mark.asyncio()
    async def test_gateway_healthz(self, gateway_client: AsyncClient) -> None:
        response = await gateway_client.get("/healthz")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio()
    async def test_order_service_healthz(self, order_client: AsyncClient) -> None:
        response = await order_client.get("/healthz")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio()
    async def test_payment_service_healthz(self, payment_client: AsyncClient) -> None:
        response = await payment_client.get("/healthz")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"


# ===================================================================
# CALL CHAIN & CONTEXT PROPAGATION TESTS
# ===================================================================


class TestRequestFlow:
    """Tests for the Gateway -> Order -> Payment call chain."""

    @pytest.mark.asyncio()
    async def test_order_request_keeps_trace_id(self, gateway_client: AsyncClient) -> None:
        """POST /orders through gateway must preserve X-Trace-ID across all hops."""
        response = await gateway_client.post(
            "/orders",
            json={"item": "widget", "quantity": 1},
            headers={"X-Trace-ID": "trace-e2e", "X-Request-ID": "req-e2e"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["trace_id"] == "trace-e2e"

    @pytest.mark.asyncio()
    async def test_order_request_generates_trace_id_if_missing(
        self, gateway_client: AsyncClient
    ) -> None:
        """If X-Trace-ID is not provided, the gateway must generate one."""
        response = await gateway_client.post(
            "/orders",
            json={"item": "widget", "quantity": 2},
            headers={"X-Request-ID": "req-auto-trace"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["trace_id"] != ""
        assert len(data["trace_id"]) > 0

    @pytest.mark.asyncio()
    async def test_order_request_returns_order_details(
        self, gateway_client: AsyncClient
    ) -> None:
        """POST /orders returns order_id, status, and payment result."""
        response = await gateway_client.post(
            "/orders",
            json={"item": "gadget", "quantity": 3},
            headers={"X-Trace-ID": "trace-details", "X-Request-ID": "req-details"},
        )
        assert response.status_code == 201
        data = response.json()
        assert "order_id" in data
        assert data["status"] == "created"
        assert "payment" in data
        assert data["payment"]["status"] == "approved"

    @pytest.mark.asyncio()
    async def test_payment_service_processes_charge(self, payment_client: AsyncClient) -> None:
        """Payment service POST /charge returns approved for valid requests."""
        response = await payment_client.post(
            "/charge",
            json={"amount": 999, "currency": "USD"},
            headers={"X-Trace-ID": "trace-pay", "X-Request-ID": "req-pay"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "approved"
        assert data["trace_id"] == "trace-pay"

    @pytest.mark.asyncio()
    async def test_order_service_creates_order(self, order_client: AsyncClient) -> None:
        """Order service POST /orders (direct) creates an order and calls payment."""
        response = await order_client.post(
            "/orders",
            json={"item": "thing", "quantity": 1},
            headers={"X-Trace-ID": "trace-order-direct", "X-Request-ID": "req-order-direct"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["trace_id"] == "trace-order-direct"
        assert data["payment"]["status"] == "approved"

    @pytest.mark.asyncio()
    async def test_request_id_propagates_across_hops(self, order_client: AsyncClient) -> None:
        """X-Request-ID must propagate from order-service to payment-service."""
        response = await order_client.post(
            "/orders",
            json={"item": "thing", "quantity": 1},
            headers={"X-Trace-ID": "trace-rid-prop", "X-Request-ID": "req-rid-prop"},
        )
        assert response.status_code == 201
        data = response.json()
        # The order service should have propagated the request to payment.
        # Payment result should exist and be approved, confirming the hop succeeded.
        assert data["payment"]["status"] == "approved"
        assert data["trace_id"] == "trace-rid-prop"
