"""Payment service — processes charge requests.

Endpoints:
  - POST /charge: process a payment charge
  - GET /healthz: health check
"""

from __future__ import annotations

import asyncio
import os
import random
import time
import uuid
from typing import Any

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from incidentlens_service_common.context import extract_context
from incidentlens_service_common.runtime_client import RuntimeConfigClient
from incidentlens_service_common.telemetry_client import TelemetryClient
from pydantic import BaseModel

app = FastAPI(title="Payment Service", version="0.1.0")

# Control plane URL for runtime config (set in Compose mode)
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "")

_telemetry = TelemetryClient("payment-service", control_plane_url=CONTROL_PLANE_URL or None)

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
        _runtime_client = RuntimeConfigClient(CONTROL_PLANE_URL, "payment-service")
    return _runtime_client


async def _get_active_scenarios() -> dict[str, dict[str, Any]]:
    """Get active scenarios for this service.

    In Compose mode (CONTROL_PLANE_URL set), fetches from the control plane.
    In test mode (_scenario_service set), uses the in-process ScenarioService.
    Returns empty dict if neither is available.
    """
    if _scenario_service is not None:
        return _scenario_service.active_for("payment-service")
    client = _get_runtime_client()
    if client is not None:
        return await client.get_active()
    return {}


class ChargeRequest(BaseModel):
    amount: int
    currency: str = "USD"


class ChargeResponse(BaseModel):
    charge_id: str
    status: str
    trace_id: str
    amount: int
    currency: str


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/charge", response_model=ChargeResponse)
async def charge(
    body: ChargeRequest,
    x_request_id: str | None = Header(None, alias="X-Request-ID"),
    x_trace_id: str | None = Header(None, alias="X-Trace-ID"),
) -> ChargeResponse | JSONResponse:
    # Extract/propagate context
    headers = {}
    if x_request_id:
        headers["x-request-id"] = x_request_id
    if x_trace_id:
        headers["x-trace-id"] = x_trace_id
    ctx = extract_context(headers)
    trace_id = ctx["X-Trace-ID"]

    start_time = time.monotonic()
    span_id = f"span-pay-{uuid.uuid4().hex[:8]}"
    _telemetry.emit_span(trace_id, span_id, "POST /charge")
    _telemetry.emit_log(trace_id, "INFO", f"Processing charge: {body.amount} {body.currency}")

    # Fetch active scenarios (from control plane or in-process service)
    active = await _get_active_scenarios()

    # Apply fault scenarios
    # payment_delay: add real delay
    params = active.get("payment_delay")
    if params is not None:
        delay_ms = params.get("delay_ms", 200)
        await asyncio.sleep(delay_ms / 1000.0)
        _telemetry.emit_span(
            trace_id,
            f"{span_id}-complete",
            "POST /charge complete",
            parent_id=span_id,
            duration_ms=delay_ms,
        )
        _telemetry.emit_metric(trace_id, "payment_latency_ms", float(delay_ms))
        _telemetry.emit_log(
            trace_id,
            "WARN",
            f"Payment processing delay observed: {delay_ms}ms",
            duration_ms=delay_ms,
        )

    # payment_error_rate: return 500 at configured rate
    params = active.get("payment_error_rate")
    if params is not None:
        error_rate = params.get("error_rate", 0.3)
        if random.random() < error_rate:
            duration_ms = (time.monotonic() - start_time) * 1000
            _telemetry.emit_log(
                trace_id, "ERROR", "Payment failed due to injected error rate",
                duration_ms=duration_ms,
                error_type="injected_error_rate",
                span_status="ERROR",
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "payment processing error", "trace_id": trace_id},
            )

    # deployment_regression: simulate buggy deployment
    params = active.get("deployment_regression")
    if params is not None:
        version = str(params.get("version", "unknown"))
        _telemetry.emit_deployment(trace_id, version)
        _telemetry.emit_log(
            trace_id, "WARN", f"Running buggy version: {version}"
        )
        # Buggy deployment returns wrong amounts
        return ChargeResponse(
            charge_id=f"chg-{uuid.uuid4().hex[:8]}",
            status="approved",
            trace_id=trace_id,
            amount=0,  # Bug: amount is zero
            currency=body.currency,
        )

    _telemetry.emit_metric(trace_id, "charge_amount", float(body.amount))
    _telemetry.emit_log(trace_id, "INFO", f"Charge approved: {body.amount} {body.currency}")

    return ChargeResponse(
        charge_id=f"chg-{uuid.uuid4().hex[:8]}",
        status="approved",
        trace_id=trace_id,
        amount=body.amount,
        currency=body.currency,
    )
