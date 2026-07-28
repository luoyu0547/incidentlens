#!/usr/bin/env python3
"""IncidentLens Demo Runner CLI.

Runs end-to-end demo scenarios via public APIs.

Usage:
    python scripts/run_demo.py --scenario payment_delay
    python scripts/run_demo.py --all
    python scripts/run_demo.py --all --control-plane-url http://localhost:8003 --gateway-url http://localhost:8000 --traffic-count 5

Mutually exclusive:
    --scenario NAME   Run a single scenario
    --all             Run all scenarios
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from incidentlens_demo.runner import SCENARIO_NAMES, DemoRunner, DemoRunResult


def _format_result(result: DemoRunResult) -> str:
    """Format a DemoRunResult for human-readable output.

    Includes: scenario, status, incident_id, root_service, cause, evidence_ids.
    Never includes root_cause_label.
    """
    parts = [f"{result.scenario}: {result.status}"]

    if result.incident_id:
        parts.append(f"  incident_id: {result.incident_id}")

    if result.report:
        root_service = result.report.get("root_service", "N/A")
        root_cause = result.report.get("root_cause", "N/A")
        evidence_ids = result.report.get("evidence_ids", [])
        rounds = result.report.get("rounds_completed", "N/A")

        parts.append(f"  root_service: {root_service}")
        parts.append(f"  root_cause: {root_cause}")
        parts.append(f"  evidence_ids: {evidence_ids}")
        parts.append(f"  rounds_completed: {rounds}")
    elif result.failure_stage:
        parts.append(f"  failure_stage: {result.failure_stage}")
        if result.failure_message:
            parts.append(f"  failure_message: {result.failure_message}")

    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the demo CLI."""
    parser = argparse.ArgumentParser(
        description="Run IncidentLens end-to-end demo scenarios",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--scenario",
        choices=SCENARIO_NAMES,
        help="Run a single scenario",
    )
    group.add_argument(
        "--all",
        action="store_true",
        dest="run_all",
        help="Run all scenarios",
    )
    parser.add_argument(
        "--control-plane-url",
        type=str,
        default="http://localhost:8003",
        help="Control plane URL (default: http://localhost:8003)",
    )
    parser.add_argument(
        "--gateway-url",
        type=str,
        default="http://localhost:8000",
        help="Gateway URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--traffic-count",
        type=int,
        default=3,
        help="Number of orders to send (default: 3)",
    )
    parser.add_argument(
        "--compose",
        action="store_true",
        help="Use deterministic params for Docker Compose mode",
    )

    args = parser.parse_args(argv)

    runner = DemoRunner(
        control_plane_url=args.control_plane_url,
        gateway_url=args.gateway_url,
        traffic_count=args.traffic_count,
        compose=args.compose,
    )

    if args.run_all:
        results = asyncio.run(runner.run_all())
    else:
        results = [asyncio.run(runner.run(args.scenario))]

    # Print results
    for result in results:
        print(_format_result(result))
        print()  # Blank line between results

    # Return nonzero if any scenario failed
    any_failed = any(r.status == "failed" for r in results)
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
