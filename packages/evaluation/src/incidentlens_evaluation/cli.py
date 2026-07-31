"""CLI for running evaluations.

Usage:
    python -m incidentlens_evaluation.cli --strategy all --scenario all
    python -m incidentlens_evaluation.cli --strategy incidentlens_verified --scenario payment_delay
"""

from __future__ import annotations

import argparse
import json

from incidentlens_control_plane.evaluations.store import EvaluationRunStore
from incidentlens_telemetry.database import create_engine

from incidentlens_evaluation.runner import run_evaluation


def main(argv: list[str] | None = None) -> None:
    """Run evaluation CLI."""
    parser = argparse.ArgumentParser(
        description="Run IncidentLens evaluations and persist results"
    )
    parser.add_argument(
        "--strategy",
        choices=["react_no_memory", "memory_unverified", "incidentlens_verified", "all"],
        required=True,
        help="Evaluation strategy to run",
    )
    parser.add_argument(
        "--scenario",
        choices=[
            "payment_delay",
            "payment_error_rate",
            "db_pool_exhaustion",
            "dependency_unavailable",
            "deployment_regression",
            "all",
        ],
        required=True,
        help="Scenario to evaluate against",
    )
    parser.add_argument(
        "--database-url",
        default="sqlite:///control_plane.db",
        help="Database URL for persisting results",
    )

    args = parser.parse_args(argv)

    # Create store
    engine = create_engine(args.database_url)
    store = EvaluationRunStore(engine)

    # Determine strategies to run
    strategies = (
        ["react_no_memory", "memory_unverified", "incidentlens_verified"]
        if args.strategy == "all"
        else [args.strategy]
    )

    results: list[dict[str, object]] = []
    for strategy in strategies:
        try:
            result = run_evaluation(
                strategy, args.scenario, store=store
            )
            results.append({
                "strategy": strategy,
                "scenario": args.scenario,
                "metrics": result.model_dump(),
            })
        except Exception as exc:
            results.append({
                "strategy": strategy,
                "scenario": args.scenario,
                "error": str(exc),
            })

    # Print JSON output (never print scenario root_cause_label)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
