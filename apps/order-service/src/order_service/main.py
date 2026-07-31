"""Order service — creates orders and calls payment service.

Endpoints:
  - POST /orders: create an order and process payment
  - GET /healthz: health check
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from incidentlens_service_common.context import extract_context, propagate_headers
from incidentlens_service_common.runtime_client import RuntimeConfigClient
from incidentlens_service_common.telemetry_client import TelemetryClient
from pydantic import BaseModel

app = FastAPI(title="Order Service", version="0.1.0")

# Control plane URL for runtime config (set in Compose mode)
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "")

_telemetry = TelemetryClient("order-service", control_plane_url=CONTROL_PLANE_URL or None)

# Payment service URL (configurable via env var, defaults to localhost)
PAYMENT_SERVICE_URL = os.environ.get("PAYMENT_SERVICE_URL", "http://localhost:8002")

# Module-level scenario service reference (set via set_scenario_service for in-process tests)
_scenario_service: Any | None = None

# Runtime config client (created when CONTROL_PLANE_URL is set)
_runtime_client: RuntimeConfigClient | None = None


def set_scenario_service(svc: Any) -> None:
    """Set the scenario service for fault injection (used by tests)."""
    global _scenario_service
    _scenario_service = svc


def _get_runtime_client() -> RuntimeConfigClient | None:
    """Get or create the runtime config client (Compose mode only)."""
    global _runtime_client
    if not CONTROL_PLANE_URL:
        return None
    if _runtime_client is None:
        _runtime_client = RuntimeConfigClient(CONTROL_PLANE_URL, "order-service")
    return _runtime_client


async def _get_active_scenarios() -> dict[str, dict[str, Any]]:
    """Get active scenarios for this service.

    In Compose mode (CONTROL_PLANE_URL set), fetches from the control plane.
    In test mode (_scenario_service set), uses the in-process ScenarioService.
    Returns empty dict if neither is available.
    """
    if _scenario_service is not None:
        return _scenario_service.active_for("order-service")
    client = _get_runtime_client()
    if client is not None:
        return await client.get_active()
    return {}


class OrderRequest(BaseModel):
    item: str
    quantity: int = 1


class OrderResponse(BaseModel):
    order_id: str
    status: str
    trace_id: str
    item: str
    quantity: int
    payment: dict[str, Any]


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/orders", response_model=OrderResponse, status_code=201)
async def create_order(
    body: OrderRequest,
    x_request_id: str | None = Header(None, alias="X-Request-ID"),
    x_trace_id: str | None = Header(None, alias="X-Trace-ID"),
) -> OrderResponse | JSONResponse:
    # Extract/propagate context
    headers = {}
    if x_request_id:
        headers["x-request-id"] = x_request_id
    if x_trace_id:
        headers["x-trace-id"] = x_trace_id
    ctx = extract_context(headers)
    trace_id = ctx["X-Trace-ID"]

    start_time = time.monotonic()
    span_id = f"span-order-{uuid.uuid4().hex[:8]}"
    _telemetry.emit_span(trace_id, span_id, "POST /orders")
    _telemetry.emit_log(trace_id, "INFO", f"Creating order: {body.item} x{body.quantity}")

    # Fetch active scenarios (from control plane or in-process service)
    active = await _get_active_scenarios()

    # Apply fault scenarios
    # db_pool_exhaustion: simulate connection pool exhaustion
    params = active.get("db_pool_exhaustion")
    if params is not None:
        _telemetry.emit_log(trace_id, "WARN", "DB pool exhausted, simulating slow query")
        pool_size = params.get("pool_size", 2)
        delay = max(0.1, 1.0 / pool_size)
        await asyncio.sleep(delay)

    # dependency_unavailable: return 502 without calling payment
    params = active.get("dependency_unavailable")
    if params is not None:
        duration_ms = (time.monotonic() - start_time) * 1000
        _telemetry.emit_log(
            trace_id, "ERROR", f"Dependency unavailable: {params.get('dependency', 'unknown')}",
            duration_ms=duration_ms,
            error_type="dependency_unavailable",
            span_status="ERROR",
        )
        return JSONResponse(
            status_code=502,
            content={
                "detail": "upstream service unavailable",
                "trace_id": trace_id,
            },
        )

    # Call payment service
    order_id = f"ord-{uuid.uuid4().hex[:8]}"
    payment_span_id = f"span-pay-call-{uuid.uuid4().hex[:8]}"
    _telemetry.emit_span(
        trace_id, payment_span_id, "POST /charge", parent_id=span_id
    )

    outgoing_headers = propagate_headers(ctx)
    payment_data = {
        "amount": body.quantity * 1000,  # Simple pricing: 1000 per item
        "currency": "USD",
    }

    # Use httpx to call payment service
    # In test mode, we use the ASGI transport directly
    payment_result = await _call_payment_service(
        payment_data, outgoing_headers
    )

    _telemetry.emit_metric(trace_id, "order_created", 1.0)
    _telemetry.emit_log(trace_id, "INFO", f"Order created: {order_id}")

    return OrderResponse(
        order_id=order_id,
        status="created",
        trace_id=trace_id,
        item=body.item,
        quantity=body.quantity,
        payment=payment_result,
    )


async def _call_payment_service(
    data: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    """Call the payment service to process a charge.

    When PAYMENT_SERVICE_URL env var is set (non-empty), uses real HTTP
    transport to that URL — suitable for production / Docker deployment.
    Otherwise falls back to ASGI transport for fast in-process testing.
    """
    if PAYMENT_SERVICE_URL:
        async with httpx.AsyncClient(base_url=PAYMENT_SERVICE_URL) as client:
            response = await client.post("/charge", json=data, headers=headers)
            return response.json()
    else:
        from payment_service.main import app as payment_app

        transport = httpx.ASGITransport(app=payment_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://payment") as client:
            response = await client.post("/charge", json=data, headers=headers)
            return response.json()
