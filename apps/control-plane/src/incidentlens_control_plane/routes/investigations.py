"""Investigation API routes for the control plane.

Provides:
  - POST /api/investigations/start — start a new investigation from an alert
  - POST /api/investigations/{incident_id}/round — run one investigation round
  - POST /api/investigations/{incident_id}/resume — resume an investigation
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from incidentlens_control_plane.events import SSEEvent, _global_bus

router = APIRouter(prefix="/api/investigations", tags=["investigations"])

# Engine is set by main.py during app startup
_engine: Any = None


def set_engine(engine: Any) -> None:
    """Set the investigation engine for the routes."""
    global _engine
    _engine = engine


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class StartInvestigationRequest(BaseModel):
    """Request body for starting a new investigation."""

    service: str
    error_rate: float | None = None
    symptom: str | None = None
    trace_id: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class InvestigationStateResponse(BaseModel):
    """Response model for investigation state."""

    incident_id: str
    status: str
    current_round: int
    max_rounds: int
    phase: str
    hypothesis_count: int
    evidence_count: int
    report: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.post("/start", status_code=status.HTTP_201_CREATED)
async def start_investigation(
    request: StartInvestigationRequest,
) -> InvestigationStateResponse:
    """Start a new investigation from an alert."""
    if _engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Investigation engine not configured",
        )

    alert: dict[str, Any] = {"service": request.service}
    if request.error_rate is not None:
        alert["error_rate"] = request.error_rate
    if request.symptom is not None:
        alert["symptom"] = request.symptom
    if request.trace_id is not None:
        alert["trace_id"] = request.trace_id
    alert.update(request.extra)

    state = _engine.start(alert)

    # Publish SSE event for the initial state
    _global_bus.publish(state.incident_id, SSEEvent(
        event_type="state_changed",
        data={
            "status": state.status.value if hasattr(state.status, "value") else str(state.status),
            "round": state.current_round,
            "phase": state.phase,
        },
    ))

    return InvestigationStateResponse(
        incident_id=state.incident_id,
        status=state.status.value,
        current_round=state.current_round,
        max_rounds=state.max_rounds,
        phase=state.phase,
        hypothesis_count=len(state.hypotheses),
        evidence_count=len(state.evidence),
        report=state.report,
    )


@router.post("/{incident_id}/round")
async def run_round(incident_id: str) -> InvestigationStateResponse:
    """Run one round of the investigation loop."""
    if _engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Investigation engine not configured",
        )

    try:
        state = await _engine.run_round(incident_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )

    # Publish SSE events for the state change
    _global_bus.publish(incident_id, SSEEvent(
        event_type="state_changed",
        data={
            "status": state.status.value if hasattr(state.status, "value") else str(state.status),
            "round": state.current_round,
            "phase": state.phase,
        },
    ))

    # Publish evidence_recorded events for new evidence
    if state.evidence:
        latest_evidence = state.evidence[-1]
        _global_bus.publish(incident_id, SSEEvent(
            event_type="evidence_recorded",
            data={
                "source_tool": latest_evidence.source_tool,
                "content": latest_evidence.content,
            },
        ))

    # Publish report_ready if applicable
    if (
        (state.status.value if hasattr(state.status, "value") else str(state.status))
        == "report_ready"
        and state.report
    ):
        _global_bus.publish(incident_id, SSEEvent(
            event_type="report_ready",
            data=state.report,
        ))

    return InvestigationStateResponse(
        incident_id=state.incident_id,
        status=state.status.value,
        current_round=state.current_round,
        max_rounds=state.max_rounds,
        phase=state.phase,
        hypothesis_count=len(state.hypotheses),
        evidence_count=len(state.evidence),
        report=state.report,
    )


@router.post("/{incident_id}/resume")
async def resume_investigation(incident_id: str) -> InvestigationStateResponse:
    """Resume an investigation from the last checkpoint."""
    if _engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Investigation engine not configured",
        )

    state = await _engine.resume(incident_id)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Investigation not found: {incident_id}",
        )

    return InvestigationStateResponse(
        incident_id=state.incident_id,
        status=state.status.value,
        current_round=state.current_round,
        max_rounds=state.max_rounds,
        phase=state.phase,
        hypothesis_count=len(state.hypotheses),
        evidence_count=len(state.evidence),
        report=state.report,
    )
