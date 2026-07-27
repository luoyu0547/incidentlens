"""Gateway service — proxies requests to order service.

Endpoints:
  - POST /orders: proxy to order service
  - GET /healthz: health check
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Header
from incidentlens_service_common.context import extract_context, propagate_headers
from incidentlens_service_common.telemetry_client import TelemetryClient
from pydantic import BaseModel

app = FastAPI(title="Gateway Service", version="0.1.0")

_telemetry = TelemetryClient("gateway-service")

# Order service URL (configurable via env var, defaults to localhost)
ORDER_SERVICE_URL = os.environ.get("ORDER_SERVICE_URL", "http://localhost:8001")


class OrderRequest(BaseModel):
    item: str
    quantity: int = 1


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/orders", status_code=201)
async def create_order(
    body: OrderRequest,
    x_request_id: str | None = Header(None, alias="X-Request-ID"),
    x_trace_id: str | None = Header(None, alias="X-Trace-ID"),
) -> dict[str, Any]:
    # Extract/propagate context
    headers = {}
    if x_request_id:
        headers["x-request-id"] = x_request_id
    if x_trace_id:
        headers["x-trace-id"] = x_trace_id
    ctx = extract_context(headers)
    trace_id = ctx["X-Trace-ID"]

    span_id = f"span-gw-{uuid.uuid4().hex[:8]}"
    _telemetry.emit_span(trace_id, span_id, "POST /orders (gateway)")
    _telemetry.emit_log(trace_id, "INFO", f"Gateway received order request: {body.item}")

    # Propagate headers to order service
    outgoing_headers = propagate_headers(ctx)

    # Call order service
    order_result = await _call_order_service(
        body.model_dump(), outgoing_headers
    )

    _telemetry.emit_metric(trace_id, "gateway_request", 1.0)
    _telemetry.emit_log(trace_id, "INFO", "Gateway completed order request")

    return order_result


async def _call_order_service(
    data: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    """Call the order service.

    When ORDER_SERVICE_URL env var is set (non-empty), uses real HTTP
    transport to that URL — suitable for production / Docker deployment.
    Otherwise falls back to ASGI transport for fast in-process testing.
    """
    if ORDER_SERVICE_URL:
        async with httpx.AsyncClient(base_url=ORDER_SERVICE_URL) as client:
            response = await client.post("/orders", json=data, headers=headers)
            return response.json()
    else:
        from order_service.main import app as order_app

        transport = httpx.ASGITransport(app=order_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://order") as client:
            response = await client.post("/orders", json=data, headers=headers)
            return response.json()
