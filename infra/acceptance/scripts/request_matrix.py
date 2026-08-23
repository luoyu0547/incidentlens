"""Run the controlled four-path request matrix and emit JSONL cells."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid

CELLS = (("stable", 10), ("stable", 500), ("canary", 10), ("canary", 500))
PRE_REPAIR = {("stable", 10): 201, ("stable", 500): 429, ("canary", 10): 503, ("canary", 500): 503}
REPAIRED = {cell: 201 for cell in CELLS}


def build_request_id(route: str, amount: int, index: int, run_nonce: str) -> str:
    """Return an ID unique to one matrix execution and stable within that run."""
    return f"matrix-{run_nonce}-{route}-{amount}-{index}"


def request_cell(
    base_url: str,
    route: str,
    amount: int,
    index: int,
    run_nonce: str,
) -> dict[str, object]:
    request_id = build_request_id(route, amount, index, run_nonce)
    payload = json.dumps({"user_id": "matrix", "total": amount}).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/orders",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
            "X-Route-Key": "route-b" if route == "canary" else "route-a",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            status = response.status
            data = json.loads(response.read())
            served_by = response.headers.get("X-Served-By") or data.get("served_by")
    except urllib.error.HTTPError as error:
        status = error.code
        data = json.loads(error.read())
        served_by = error.headers.get("X-Served-By") or data.get("served_by")
    return {
        "route": route,
        "amount": amount,
        "request_id": request_id,
        "status": status,
        "served_by": served_by,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--expected", choices=("pre-repair", "repaired"), required=True)
    args = parser.parse_args()
    expected = PRE_REPAIR if args.expected == "pre-repair" else REPAIRED
    run_nonce = uuid.uuid4().hex[:12]
    ok = True
    for index, (route, amount) in enumerate(CELLS, 1):
        cell = request_cell(args.url, route, amount, index, run_nonce)
        print(json.dumps(cell, sort_keys=True), flush=True)
        ok = ok and cell["status"] == expected[(route, amount)] and cell["served_by"] == route
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
