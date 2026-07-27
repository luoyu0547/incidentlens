"""Order service — creates orders and calls payment service.

Endpoints:
  - POST /orders: create an order and process payment
  - GET /healthz: health check
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from incidentlens_service_common.context import extract_context, propagate_headers
from incidentlens_service_common.telemetry_client import TelemetryClient
from pydantic import BaseModel

app = FastAPI(title="Order Service", version="0.1.0")

_telemetry = TelemetryClient("order-service")

# Payment service URL (configurable via env var, defaults to localhost)
PAYMENT_SERVICE_URL = os.environ.get("PAYMENT_SERVICE_URL", "http://localhost:8002")

# Module-level scenario service reference (set via set_scenario_service)
_scenario_service: Any | None = None


def set_scenario_service(svc: Any) -> None:
    """Set the scenario service for fault injection (used by tests)."""
    global _scenario_service
    _scenario_service = svc


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
) -> OrderResponse:
    # Extract/propagate context
    headers = {}
    if x_request_id:
        headers["x-request-id"] = x_request_id
    if x_trace_id:
        headers["x-trace-id"] = x_trace_id
    ctx = extract_context(headers)
    trace_id = ctx["X-Trace-ID"]

    span_id = f"span-order-{uuid.uuid4().hex[:8]}"
    _telemetry.emit_span(trace_id, span_id, "POST /orders")
    _telemetry.emit_log(trace_id, "INFO", f"Creating order: {body.item} x{body.quantity}")

    # Apply fault scenarios
    if _scenario_service is not None:
        # db_pool_exhaustion: simulate connection pool exhaustion
        params = _scenario_service.get_params("db_pool_exhaustion")
        if params is not None:
            _telemetry.emit_log(trace_id, "WARN", "DB pool exhausted, simulating slow query")
            pool_size = params.get("pool_size", 2)
            delay = max(0.1, 1.0 / pool_size)
            await asyncio.sleep(delay)

        # dependency_unavailable: return 502 without calling payment
        params = _scenario_service.get_params("dependency_unavailable")
        if params is not None:
            _telemetry.emit_log(
                trace_id, "ERROR", f"Dependency unavailable: {params.get('dependency', 'unknown')}"
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
