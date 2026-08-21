# infra/acceptance/services/payment-service/app.py
"""Payment service with a repairable policy setting."""

import logging
import os

from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("payment-service")
POLICY_VERSION = os.environ.get("PAYMENT_POLICY_VERSION", "2026-08-policy-a")
REJECT_ABOVE = float(os.environ.get("PAYMENT_REJECT_ABOVE", "1000000"))


@app.route("/health")
def health():
    return jsonify({"status": "ok", "policy_version": POLICY_VERSION})


@app.route("/payments", methods=["POST"])
def create_payment():
    data = request.get_json(silent=True) or {}
    request_id = request.headers.get("X-Request-ID", "missing")
    amount = float(data.get("amount", 0))
    if amount > REJECT_ABOVE:
        logger.warning(
            "payment decision=reject request_id=%s policy_version=%s amount=%.2f",
            request_id,
            POLICY_VERSION,
            amount,
        )
        return jsonify({"error": "payment processing declined", "request_id": request_id}), 429
    logger.info(
        "payment decision=allow request_id=%s policy_version=%s amount=%.2f",
        request_id,
        POLICY_VERSION,
        amount,
    )
    return jsonify({"status": "processed", "request_id": request_id}), 201


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
