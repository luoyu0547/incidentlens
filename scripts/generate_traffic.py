#!/usr/bin/env python3
"""Generate HTTP traffic against the IncidentLens gateway service.

Sends a configurable number of order requests through the gateway,
which propagates to order and payment services, generating telemetry.

Returns request summaries and trace IDs for downstream use.

Usage:
    python scripts/generate_traffic.py [--count 20] [--url http://localhost:8000]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class TrafficSummary:
    """Summary of traffic generation results.

    Attributes:
        success_count: Number of successful order requests.
        error_count: Number of failed order requests.
        trace_ids: List of trace IDs sent with the requests.
    """

    success_count: int = 0
    error_count: int = 0
    trace_ids: list[str] = field(default_factory=list)


async def send_order(
    client: httpx.AsyncClient,
    base_url: str,
    item: str,
    quantity: int,
) -> dict[str, Any] | None:
    """Send a single order request to the gateway."""
    trace_id = f"trace-{time.time_ns()}"
    try:
        response = await client.post(
            f"{base_url}/orders",
            json={"item": item, "quantity": quantity},
            headers={
                "X-Request-ID": f"req-{time.time_ns()}",
                "X-Trace-ID": trace_id,
            },
        )
        return response.json()
    except Exception as exc:
        print(f"  Error sending order: {exc}", file=sys.stderr)
        return None


async def generate_traffic(count: int, base_url: str) -> TrafficSummary:
    """Generate traffic by sending order requests.

    Returns a TrafficSummary with success/error counts and trace IDs.
    """
    print(f"Generating {count} orders against {base_url}")

    summary = TrafficSummary()
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        # Check health first
        try:
            resp = await client.get("/healthz")
            if resp.status_code != 200:
                print(f"Gateway not healthy: {resp.status_code}", file=sys.stderr)
                return summary
        except Exception as exc:
            print(f"Cannot reach gateway at {base_url}: {exc}", file=sys.stderr)
            return summary

        items = ["widget", "gadget", "doohickey", "thingamajig", "whatchamacallit"]

        for i in range(count):
            item = items[i % len(items)]
            trace_id = f"trace-{time.time_ns()}"
            summary.trace_ids.append(trace_id)
            result = await send_order(client, base_url, item, (i % 3) + 1)
            if result and "order_id" in result:
                summary.success_count += 1
                print(f"  [{i+1}/{count}] Order created: {result.get('order_id', 'N/A')}")
            else:
                summary.error_count += 1
                print(f"  [{i+1}/{count}] Order failed: {result}")

            # Small delay between requests
            await asyncio.sleep(0.1)

    print(f"\nDone: {summary.success_count} succeeded, {summary.error_count} failed out of {count}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate traffic for IncidentLens demo")
    parser.add_argument("--count", type=int, default=20, help="Number of orders to send")
    parser.add_argument("--url", type=str, default="http://localhost:8000", help="Gateway URL")
    args = parser.parse_args()

    asyncio.run(generate_traffic(args.count, args.url))


if __name__ == "__main__":
    main()
