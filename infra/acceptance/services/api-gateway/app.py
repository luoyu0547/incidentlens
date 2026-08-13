# infra/acceptance/services/api-gateway/app.py
"""模拟 API 网关：统一入口，将请求转发到下游微服务。"""

import os
import logging
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("api-gateway")

ORDER_URL = os.environ.get("ORDER_URL", "http://localhost:5001")
PAYMENT_URL = os.environ.get("PAYMENT_URL", "http://localhost:5002")
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://localhost:5003")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


def _forward(base_url, path, timeout=5):
    """Forward the incoming request body to a downstream service."""
    try:
        resp = requests.post(f"{base_url}{path}", json=request.json, timeout=timeout)
        logger.info("Forwarded %s -> %s (status=%s)", path, base_url, resp.status_code)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"status": resp.text or "empty"}
        return jsonify(payload), resp.status_code
    except requests.exceptions.RequestException as e:
        logger.error("ERROR: Downstream %s unavailable: %s", base_url, str(e))
        return jsonify({"error": f"downstream service unavailable: {str(e)}"}), 502


@app.route("/orders", methods=["POST"])
def create_order():
    return _forward(ORDER_URL, "/orders")


@app.route("/payments", methods=["POST"])
def create_payment():
    return _forward(PAYMENT_URL, "/payments")


@app.route("/inventory/reserve", methods=["POST"])
def reserve_inventory():
    return _forward(INVENTORY_URL, "/inventory/reserve")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
