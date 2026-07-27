"""Scenario API routes for the control plane.

Provides:
  - GET /api/scenarios — list all scenario definitions
  - POST /api/scenarios/{name}/enable — activate a scenario
  - POST /api/scenarios/{name}/disable — deactivate a scenario
  - POST /api/scenarios/reset — reset all scenarios and demo data
  - GET /api/scenarios/runtime/{service} — get active scenarios for a service
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from incidentlens_scenarios.models import SCENARIOS
from incidentlens_scenarios.store import ScenarioStore

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])

# Dependencies set by main.py during app startup
_store: ScenarioStore | None = None
_reset_service: Any = None


def set_scenario_store(store: ScenarioStore) -> None:
    """Set the scenario store for the routes."""
    global _store
    _store = store


def set_demo_reset_service(service: Any) -> None:
    """Set the demo reset service for the routes."""
    global _reset_service
    _reset_service = service


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class EnableScenarioRequest(BaseModel):
    """Request body for enabling a scenario. All fields are optional overrides."""

    delay_ms: int | float | None = None
    error_rate: float | None = None
    pool_size: int | None = None
    dependency: str | None = None
    version: str | None = None


class ScenarioStatusResponse(BaseModel):
    """Response for enable/disable operations."""

    name: str
    active: bool
    parameters: dict[str, Any] | None = None


class ScenarioDefinitionResponse(BaseModel):
    """Response for a scenario definition (no root_cause_label)."""

    name: str
    target_service: str
    default_params: dict[str, Any]


class RuntimeScenarioResponse(BaseModel):
    """Response for runtime projection of active scenarios for a service."""

    service: str
    active: dict[str, dict[str, Any]]


class ResetResponse(BaseModel):
    """Response for the reset operation."""

    status: str
    scenarios_cleared: bool
    tables_cleared: dict[str, int] | None = None


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ScenarioDefinitionResponse])
async def list_scenarios() -> list[ScenarioDefinitionResponse]:
    """List all scenario definitions (without root_cause_label)."""
    results = []
    for name, definition in SCENARIOS.items():
        # Explicitly exclude root_cause_label from the response
        safe_defaults = {
            k: v
            for k, v in definition["default_params"].items()
            if k != "root_cause_label"
        }
        results.append(
            ScenarioDefinitionResponse(
                name=name,
                target_service=definition["target_service"],
                default_params=safe_defaults,
            )
        )
    return results


@router.post("/{name}/enable", response_model=ScenarioStatusResponse)
async def enable_scenario(
    name: str,
    request: EnableScenarioRequest | None = None,
) -> ScenarioStatusResponse:
    """Activate a scenario with the given parameters."""
    if _store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scenario store not configured",
        )

    if name not in SCENARIOS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown scenario: {name}",
        )

    # Build params dict from non-None request fields
    params: dict[str, Any] = {}
    if request is not None:
        request_dict = request.model_dump(exclude_none=True)
        params = request_dict

    try:
        _store.enable(name, params)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        )

    # Return safe params (no root_cause_label)
    runtime = _store.runtime_for(SCENARIOS[name]["target_service"])
    safe_params = runtime.get(name, {})

    return ScenarioStatusResponse(
        name=name,
        active=True,
        parameters=safe_params,
    )


@router.post("/{name}/disable", response_model=ScenarioStatusResponse)
async def disable_scenario(name: str) -> ScenarioStatusResponse:
    """Deactivate a specific scenario."""
    if _store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scenario store not configured",
        )

    if name not in SCENARIOS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown scenario: {name}",
        )

    _store.disable(name)

    return ScenarioStatusResponse(
        name=name,
        active=False,
    )


@router.post("/reset", response_model=ResetResponse)
async def reset_scenarios() -> ResetResponse:
    """Reset all scenarios and demo data."""
    if _reset_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo reset service not configured",
        )

    result = _reset_service.reset_demo_data()
    return ResetResponse(**result)


@router.get("/runtime/{service}", response_model=RuntimeScenarioResponse)
async def runtime(service: str) -> RuntimeScenarioResponse:
    """Get active scenarios for a given service (safe projection)."""
    if _store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scenario store not configured",
        )

    active = _store.runtime_for(service)
    return RuntimeScenarioResponse(service=service, active=active)
