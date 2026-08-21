# infra/acceptance/services/order-service/app.py
"""Order service replica for the controlled acceptance target."""

import logging
import os

import psycopg2
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("order-service")
REPLICA_NAME = os.environ.get("REPLICA_NAME", "stable")


def get_db():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "acceptance"),
        user=os.environ.get("DB_USER", "test"),
        password=os.environ.get("DB_PASSWORD", "test"),
        connect_timeout=2,
    )


def response(payload: dict, status: int):
    result = jsonify({**payload, "served_by": REPLICA_NAME})
    result.headers["X-Served-By"] = REPLICA_NAME
    result.headers["X-Request-ID"] = request.headers.get("X-Request-ID", "")
    return result, status


@app.route("/health")
def health():
    return jsonify({"status": "ok", "replica": REPLICA_NAME})


@app.route("/orders", methods=["POST"])
def create_order():
    data = request.get_json(silent=True) or {}
    request_id = request.headers.get("X-Request-ID", "missing")
    total = data.get("total", 0)
    user_id = data.get("user_id", "anonymous")
    logger.info(
        "order request_id=%s replica=%s user=%s amount=%.2f",
        request_id,
        REPLICA_NAME,
        user_id,
        float(total),
    )
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
    except Exception:
        logger.error("database operation failed request_id=%s replica=%s", request_id, REPLICA_NAME)
        return response({"error": "order storage unavailable", "request_id": request_id}, 503)

    try:
        payment = requests.post(
            f"{os.environ.get('PAYMENT_URL', 'http://localhost:5000')}/payments",
            json={"order_id": order_id, "amount": total},
            headers={"X-Request-ID": request_id},
            timeout=5,
        )
    except requests.exceptions.RequestException:
        logger.error("payment call unavailable request_id=%s replica=%s", request_id, REPLICA_NAME)
        return response({"error": "payment unavailable", "request_id": request_id}, 502)
    if payment.status_code >= 400:
        logger.warning(
            "payment declined request_id=%s replica=%s status=%s",
            request_id,
            REPLICA_NAME,
            payment.status_code,
        )
        return response(
            {"error": "payment processing declined", "request_id": request_id},
            payment.status_code,
        )
    logger.info(
        "order completed request_id=%s replica=%s order_id=%s",
        request_id,
        REPLICA_NAME,
        order_id,
    )
    return response({"order_id": order_id, "status": "created", "request_id": request_id}, 201)


@app.route("/orders/<int:order_id>")
def get_order(order_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, user_id, total, status FROM orders WHERE id = %s", (order_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception:
        return response({"error": "order storage unavailable"}, 503)
    if row:
        return response({"id": row[0], "user_id": row[1], "total": row[2], "status": row[3]}, 200)
    return response({"error": "not found"}, 404)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
