# infra/acceptance/services/api-gateway/app.py
"""Deterministic gateway for the controlled acceptance target."""

import logging
import os
import uuid

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("api-gateway")

ORDER_STABLE_URL = os.environ.get("ORDER_STABLE_URL", "http://localhost:5001")
ORDER_CANARY_URL = os.environ.get("ORDER_CANARY_URL", "http://localhost:5002")
PAYMENT_URL = os.environ.get("PAYMENT_URL", "http://localhost:5003")
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://localhost:5004")


def _route_for(value: str | None) -> str:
    """Choose a replica from the opaque route key without exposing its meaning."""
    return "canary" if value == "route-b" else "stable"


def _request_id() -> str:
    return request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:16]}"


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


def _forward(base_url, path, *, request_id: str, extra_headers: dict[str, str] | None = None):
    headers = {"X-Request-ID": request_id}
    if extra_headers:
        headers.update(extra_headers)
    try:
        response = requests.post(f"{base_url}{path}", json=request.json, headers=headers, timeout=5)
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": "downstream response unavailable"}
        served_by = response.headers.get("X-Served-By")
        if served_by:
            payload["served_by"] = served_by
        logger.info(
            "forward request_id=%s path=%s status=%s served_by=%s",
            request_id,
            path,
            response.status_code,
            served_by or "unknown",
        )
        result = jsonify(payload)
        result.headers["X-Request-ID"] = request_id
        if served_by:
            result.headers["X-Served-By"] = served_by
        return result, response.status_code
    except requests.exceptions.RequestException:
        logger.error("downstream unavailable request_id=%s path=%s", request_id, path)
        return jsonify({"error": "downstream service unavailable", "request_id": request_id}), 502


@app.route("/orders", methods=["POST"])
def create_order():
    request_id = _request_id()
    route = _route_for(request.headers.get("X-Route-Key"))
    base_url = ORDER_CANARY_URL if route == "canary" else ORDER_STABLE_URL
    return _forward(base_url, "/orders", request_id=request_id, extra_headers={"X-Route": route})


@app.route("/payments", methods=["POST"])
def create_payment():
    return _forward(PAYMENT_URL, "/payments", request_id=_request_id())


@app.route("/inventory/reserve", methods=["POST"])
def reserve_inventory():
    return _forward(INVENTORY_URL, "/inventory/reserve", request_id=_request_id())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
