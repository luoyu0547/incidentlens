"""Evaluation API routes for the control plane.

Provides:
  - GET /api/evaluations/comparison — latest completed run per strategy with metrics
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

router = APIRouter(prefix="/api/evaluations", tags=["evaluations"])

# ---------------------------------------------------------------------------
# Injected dependencies — set by main.py or test fixtures
# ---------------------------------------------------------------------------

_evaluation_store: Any = None


def set_evaluation_store(store: Any) -> None:
    """Set the evaluation store for the routes."""
    global _evaluation_store  # noqa: PLW0603
    _evaluation_store = store


def _get_store() -> Any:
    """Return the evaluation store or raise 503."""
    if _evaluation_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Evaluation store not configured",
        )
    return _evaluation_store


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/comparison")
async def get_comparison(
    scenario: str = Query("all", description="Filter by scenario name or 'all'"),
) -> dict[str, Any]:
    """Return the latest completed evaluation run per strategy.

    Returns a list of runs with their strategy, scenario, metrics, and timestamps.
    With no completed data, returns {"runs": []}.
    """
    store = _get_store()
    runs = store.latest_comparison(scenario=scenario)
    return {"runs": runs}
