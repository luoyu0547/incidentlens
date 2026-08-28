# infra/acceptance/services/inventory-service/app.py
"""模拟库存服务。"""

import logging
import os

from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("inventory-service")

FAULT = os.environ.get("FAULT_DEPENDENCY", "false").lower() == "true"


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/inventory/reserve", methods=["POST"])
def reserve():
    data = request.json
    if FAULT:
        logger.error("ERROR: Inventory reservation failed - service unavailable")
        return jsonify({"error": "inventory service unavailable"}), 503
    logger.info("Reserved %s units for product=%s", data.get("quantity", 1), data.get("product_id"))
    return jsonify({"status": "reserved"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
