"""Tests for evaluation API routes — TDD RED phase.

GET /api/evaluations/comparison returns the latest completed run per strategy
with its metrics, timestamp, and records.
"""

from __future__ import annotations

import httpx
import pytest
from incidentlens_control_plane.evaluations.store import EvaluationRunStore
from incidentlens_control_plane.main import create_app
from sqlalchemy import create_engine


@pytest.fixture
def eval_store() -> EvaluationRunStore:
    """Create an in-memory EvaluationRunStore for API tests."""
    engine = create_engine("sqlite:///:memory:")
    return EvaluationRunStore(engine)


@pytest.fixture
async def evaluation_client(eval_store: EvaluationRunStore) -> httpx.AsyncClient:
    """AsyncClient wired with evaluation store for API tests."""
    from incidentlens_control_plane.routes.evaluations import set_evaluation_store

    set_evaluation_store(eval_store)
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


async def test_comparison_returns_200_with_empty_runs(
    evaluation_client: httpx.AsyncClient,
) -> None:
    """GET /api/evaluations/comparison returns 200 with empty runs when no data."""
    response = await evaluation_client.get(
        "/api/evaluations/comparison", params={"scenario": "all"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["runs"] == []


async def test_comparison_returns_latest_completed_per_strategy(
    eval_store: EvaluationRunStore,
    evaluation_client: httpx.AsyncClient,
) -> None:
    """GET /api/evaluations/comparison returns latest completed run per strategy."""
    # Seed completed runs for all three strategies
    for strategy in ("react_no_memory", "memory_unverified", "incidentlens_verified"):
        run_id = eval_store.start(strategy, "all")
        eval_store.record(run_id, {"root_service_expected": "payment-service"})
        eval_store.complete(run_id, {
            "root_service_accuracy": 1.0,
            "root_cause_type_accuracy": 1.0,
            "evidence_reference_correctness": 100.0,
            "first_effective_hypothesis_round": 1.0,
            "average_tool_calls": 3.0,
            "duplicate_rate": 0.0,
            "historical_case_misleading_rate": 0.0,
            "average_latency_ms": 100.0,
        })

    response = await evaluation_client.get(
        "/api/evaluations/comparison", params={"scenario": "all"}
    )
    assert response.status_code == 200
    body = response.json()
    assert {row["strategy"] for row in body["runs"]} == {
        "react_no_memory",
        "memory_unverified",
        "incidentlens_verified",
    }
    assert set(body["runs"][0]["metrics"]) == {
        "root_service_accuracy",
        "root_cause_type_accuracy",
        "evidence_reference_correctness",
        "first_effective_hypothesis_round",
        "average_tool_calls",
        "duplicate_rate",
        "historical_case_misleading_rate",
        "average_latency_ms",
    }


async def test_comparison_excludes_failed_runs(
    eval_store: EvaluationRunStore,
    evaluation_client: httpx.AsyncClient,
) -> None:
    """GET /api/evaluations/comparison excludes failed runs."""
    # Complete one strategy
    run_id1 = eval_store.start("react_no_memory", "all")
    eval_store.record(run_id1, {"root_service_expected": "payment-service"})
    eval_store.complete(run_id1, {"root_service_accuracy": 0.5})

    # Fail another strategy
    run_id2 = eval_store.start("memory_unverified", "all")
    eval_store.record(run_id2, {"root_service_expected": "payment-service"})
    eval_store.fail(run_id2, "model_timeout")

    response = await evaluation_client.get(
        "/api/evaluations/comparison", params={"scenario": "all"}
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["runs"]) == 1
    assert body["runs"][0]["strategy"] == "react_no_memory"


async def test_comparison_filters_by_scenario(
    eval_store: EvaluationRunStore,
    evaluation_client: httpx.AsyncClient,
) -> None:
    """GET /api/evaluations/comparison filters by scenario."""
    run_id1 = eval_store.start("react_no_memory", "payment_delay")
    eval_store.record(run_id1, {"root_service_expected": "payment-service"})
    eval_store.complete(run_id1, {"root_service_accuracy": 0.5})

    run_id2 = eval_store.start("react_no_memory", "db_pool_exhaustion")
    eval_store.record(run_id2, {"root_service_expected": "db-service"})
    eval_store.complete(run_id2, {"root_service_accuracy": 0.3})

    response = await evaluation_client.get(
        "/api/evaluations/comparison", params={"scenario": "payment_delay"}
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["runs"]) == 1
    assert body["runs"][0]["scenario"] == "payment_delay"
