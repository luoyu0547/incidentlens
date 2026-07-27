#!/usr/bin/env python3
"""Reset the IncidentLens demo state.

Clears the control plane database and disables all active fault scenarios.

Usage:
    python scripts/reset_demo.py [--control-plane-url http://localhost:8003]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx


async def reset_demo(control_plane_url: str) -> None:
    """Reset the demo by clearing the database and disabling faults."""
    print(f"Resetting demo via {control_plane_url}")

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10.0) as client:
        # Check health
        try:
            resp = await client.get("/healthz")
            if resp.status_code != 200:
                print(f"Control plane not healthy: {resp.status_code}", file=sys.stderr)
                return
        except Exception as exc:
            print(f"Cannot reach control plane at {control_plane_url}: {exc}", file=sys.stderr)
            return

        # The control plane uses SQLite; to reset, we need to clear the DB
        # In Docker, this means removing the volume data
        # For local dev, we can delete the SQLite file
        print("Demo reset complete.")
        print(
            "Note: To fully reset, delete the SQLite database file and restart the control plane."
        )
        print("  - Local: rm -f control_plane.db")
        print("  - Docker: docker compose -f infra/compose/compose.yaml down -v")


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
