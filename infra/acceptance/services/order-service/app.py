# infra/acceptance/services/order-service/app.py
"""模拟订单服务：接收订单，调用支付和库存服务。"""

import os
import time
import logging
from flask import Flask, request, jsonify
import psycopg2
import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("order-service")

FAULT_DB_POOL = os.environ.get("FAULT_DB_POOL", "false").lower() == "true"
FAULT_PAYMENT_TIMEOUT = os.environ.get("FAULT_PAYMENT_TIMEOUT", "false").lower() == "true"


def get_db():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "acceptance"),
        user=os.environ.get("DB_USER", "test"),
        password=os.environ.get("DB_PASSWORD", "test"),
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/orders", methods=["POST"])
def create_order():
    data = request.json
    user_id = data.get("user_id", "anonymous")
    total = data.get("total", 0)

    logger.info("Creating order for user=%s total=%.2f", user_id, total)

    if FAULT_DB_POOL:
        logger.error("ERROR: Cannot acquire database connection - pool exhausted")
        return jsonify({"error": "database connection pool exhausted"}), 503

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO orders (user_id, total) VALUES (%s, %s) RETURNING id",
            (user_id, total),
        )
        order_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Order %d created successfully", order_id)
    except Exception as e:
        logger.error("ERROR: Database error: %s", str(e))
        return jsonify({"error": str(e)}), 500

    # Call payment service
    try:
        timeout = 0.001 if FAULT_PAYMENT_TIMEOUT else 5
        resp = requests.post(
            f"{os.environ.get('PAYMENT_URL', 'http://localhost:5000')}/payments",
            json={"order_id": order_id, "amount": total},
            timeout=timeout,
        )
        logger.info("Payment response: %s", resp.status_code)
    except requests.Timeout:
        logger.error("ERROR: Payment service timeout after %.3fs", timeout)
        return jsonify({"error": "payment service timeout"}), 504
    except Exception as e:
        logger.error("ERROR: Payment service unavailable: %s", str(e))
        return jsonify({"error": str(e)}), 502

    return jsonify({"order_id": order_id, "status": "created"}), 201


@app.route("/orders/<int:order_id>")
def get_order(order_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, total, status FROM orders WHERE id = %s", (order_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return jsonify({"id": row[0], "user_id": row[1], "total": row[2], "status": row[3]})
    return jsonify({"error": "not found"}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
