"""Payment service — processes charge requests.

Endpoints:
  - POST /charge: process a payment charge
  - GET /healthz: health check
"""

from __future__ import annotations

import asyncio
import random
import uuid
from typing import Any

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from incidentlens_service_common.context import extract_context
from incidentlens_service_common.telemetry_client import TelemetryClient
from pydantic import BaseModel

app = FastAPI(title="Payment Service", version="0.1.0")

_telemetry = TelemetryClient("payment-service")

# Module-level scenario service reference (set via set_scenario_service)
_scenario_service: Any | None = None


def set_scenario_service(svc: Any) -> None:
    """Set the scenario service for fault injection (used by tests)."""
    global _scenario_service
    _scenario_service = svc


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
) -> ChargeResponse:
    # Extract/propagate context
    headers = {}
    if x_request_id:
        headers["x-request-id"] = x_request_id
    if x_trace_id:
        headers["x-trace-id"] = x_trace_id
    ctx = extract_context(headers)
    trace_id = ctx["X-Trace-ID"]

    span_id = f"span-pay-{uuid.uuid4().hex[:8]}"
    _telemetry.emit_span(trace_id, span_id, "POST /charge")
    _telemetry.emit_log(trace_id, "INFO", f"Processing charge: {body.amount} {body.currency}")

    # Apply fault scenarios
    if _scenario_service is not None:
        # payment_delay: add real delay
        params = _scenario_service.get_params("payment_delay")
        if params is not None:
            delay_ms = params.get("delay_ms", 200)
            await asyncio.sleep(delay_ms / 1000.0)

        # payment_error_rate: return 500 at configured rate
        params = _scenario_service.get_params("payment_error_rate")
        if params is not None:
            error_rate = params.get("error_rate", 0.3)
            if random.random() < error_rate:
                _telemetry.emit_log(trace_id, "ERROR", "Payment failed due to injected error rate")
                return JSONResponse(
                    status_code=500,
                    content={"detail": "payment processing error", "trace_id": trace_id},
                )

        # deployment_regression: simulate buggy deployment
        params = _scenario_service.get_params("deployment_regression")
        if params is not None:
            _telemetry.emit_log(
                trace_id, "WARN", f"Running buggy version: {params.get('version', 'unknown')}"
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
