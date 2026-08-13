# infra/acceptance/services/payment-service/app.py
"""模拟支付服务。"""

import os
import logging
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("payment-service")

FAULT = os.environ.get("FAULT_DEPENDENCY", "false").lower() == "true"


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/payments", methods=["POST"])
def create_payment():
    data = request.json
    if FAULT:
        logger.error("ERROR: Payment processing failed - external dependency unavailable")
        return jsonify({"error": "external payment gateway unavailable"}), 503
    logger.info("Payment processed for order=%s amount=%s", data.get("order_id"), data.get("amount"))
    return jsonify({"status": "processed"}), 201


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
