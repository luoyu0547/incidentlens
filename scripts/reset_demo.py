#!/usr/bin/env python3
"""Reset the IncidentLens demo state.

Clears the control plane database and disables all active fault scenarios
via the public API endpoint POST /api/scenarios/reset.

Usage:
    python scripts/reset_demo.py [--control-plane-url http://localhost:8003]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx


async def reset_demo(control_plane_url: str) -> None:
    """Reset the demo by calling the public API reset endpoint."""
    base_url = control_plane_url.rstrip("/")
    print(f"Resetting demo via {base_url}")

    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        # Check health
        try:
            resp = await client.get("/healthz")
            if resp.status_code != 200:
                print(f"Control plane not healthy: {resp.status_code}", file=sys.stderr)
                return
        except Exception as exc:
            print(f"Cannot reach control plane at {base_url}: {exc}", file=sys.stderr)
            return

        # Call the public API reset endpoint
        try:
            resp = await client.post("/api/scenarios/reset")
            if resp.status_code == 200:
                data = resp.json()
                scenarios_cleared = data.get("scenarios_cleared", False)
                tables_cleared = data.get("tables_cleared", {})
                print(f"Demo reset complete.")
                print(f"  Scenarios cleared: {scenarios_cleared}")
                if tables_cleared:
                    for table, count in tables_cleared.items():
                        print(f"  {table}: {count} rows deleted")
            else:
                print(f"Reset failed: {resp.status_code} {resp.text}", file=sys.stderr)
        except Exception as exc:
            print(f"Error calling reset endpoint: {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset IncidentLens demo state")
    parser.add_argument(
        "--control-plane-url",
        type=str,
        default="http://localhost:8003",
        help="Control plane URL",
    )
    args = parser.parse_args()

    asyncio.run(reset_demo(args.control_plane_url))


if __name__ == "__main__":
    main()
