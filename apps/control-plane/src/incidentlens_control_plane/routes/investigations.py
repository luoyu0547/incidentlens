"""Investigation API routes for the control plane.

Provides:
  - POST /api/investigations/start — start a new investigation from an alert
  - POST /api/investigations/{incident_id}/round — run one investigation round
  - POST /api/investigations/{incident_id}/resume — resume an investigation
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from incidentlens_control_plane.events import EventBus, SSEEvent

router = APIRouter(prefix="/api/investigations", tags=["investigations"])

# Engine and event bus are set by main.py during app startup
_engine: Any = None
_event_bus: EventBus | None = None
_case_service: Any = None
_export_service: Any = None

# Track which tool_call audit entries have already been published as SSE events
# to avoid duplicates when run_round is called multiple times.
# Scoped per incident_id so completed investigations can be cleaned up.
_published_tool_call_ids: dict[str, set[int]] = {}


def set_engine(engine: Any) -> None:
    """Set the investigation engine for the routes."""
    global _engine
    _engine = engine


def set_event_bus(bus: EventBus) -> None:
    """Set the event bus for publishing SSE events."""
    global _event_bus
    _event_bus = bus


def set_case_service(service: Any) -> None:
    """Set the case service for the export endpoint."""
    global _case_service
    _case_service = service


def set_export_service(service: Any) -> None:
    """Set the investigation export service for the export endpoint."""
    global _export_service
    _export_service = service


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
    mode: str = ""
    model_profile: str = ""
    model_call_count: int = 0
    tool_call_count: int = 0
    loaded_skill_names: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    last_error_code: str | None = None
    last_checkpoint_id: str | None = None
    case_id: int | None = None
    case_status: str | None = None


# ---------------------------------------------------------------------------
# Route helpers
# ---------------------------------------------------------------------------


def _build_state_response(state, *, include_mode: str = "") -> InvestigationStateResponse:
    """Build an InvestigationStateResponse from an InvestigationState."""
    return InvestigationStateResponse(
        incident_id=state.incident_id,
        status=state.status.value,
        current_round=state.current_round,
        max_rounds=state.max_rounds,
        phase=state.phase,
        hypothesis_count=len(state.hypotheses),
        evidence_count=len(state.evidence),
        report=state.report,
        mode=include_mode,
        model_profile=getattr(state, "model_profile", ""),
        model_call_count=getattr(state, "model_call_count", 0),
        tool_call_count=getattr(state, "tool_call_count", 0),
        loaded_skill_names=getattr(state, "loaded_skill_names", []),
        fallback_used=getattr(state, "fallback_used", False),
        last_error_code=getattr(state, "last_error_code", None),
        last_checkpoint_id=getattr(state, "last_checkpoint_id", None),
        case_id=getattr(state, "case_id", None),
        case_status=getattr(state, "case_status", None),
    )


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

    state = await _engine.start(alert)

    # Initialize the per-incident published tool call tracker
    _published_tool_call_ids.setdefault(state.incident_id, set())

    # Publish SSE event for the initial state
    if _event_bus is not None:
        _event_bus.publish(state.incident_id, SSEEvent(
            event_type="state_changed",
            data={
                "status": (
                    state.status.value
                    if hasattr(state.status, "value")
                    else str(state.status)
                ),
                "round": state.current_round,
                "phase": state.phase,
            },
        ))

    return _build_state_response(state, include_mode=str(getattr(_engine, "mode", "")))


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
    if _event_bus is not None:
        _event_bus.publish(incident_id, SSEEvent(
            event_type="state_changed",
            data={
                "status": (
                    state.status.value
                    if hasattr(state.status, "value")
                    else str(state.status)
                ),
                "round": state.current_round,
                "phase": state.phase,
            },
        ))

    # Publish tool_called events for any new tool calls in this round
    if _event_bus is not None and _engine is not None:
        published = _published_tool_call_ids.setdefault(incident_id, set())
        tool_call_entries = _engine.audit_store.list_for_incident(
            incident_id, action="tool_call"
        )
        for entry in tool_call_entries:
            if entry["id"] not in published:
                published.add(entry["id"])
                details = entry.get("details", {})
                _event_bus.publish(incident_id, SSEEvent(
                    event_type="tool_called",
                    data={
                        "tool": details.get("tool", "unknown"),
                        "args": details.get("args", {}),
                        "ok": details.get("ok", False),
                    },
                ))

    # Publish evidence_recorded events for new evidence
    if state.evidence and _event_bus is not None:
        latest_evidence = state.evidence[-1]
        _event_bus.publish(incident_id, SSEEvent(
            event_type="evidence_recorded",
            data={
                "source_tool": latest_evidence.source_tool,
                "content": latest_evidence.content,
            },
        ))

    # Publish report_ready if applicable
    if (
        _event_bus is not None
        and (state.status.value if hasattr(state.status, "value") else str(state.status))
        == "report_ready"
        and state.report
    ):
        _event_bus.publish(incident_id, SSEEvent(
            event_type="report_ready",
            data=state.report,
        ))

    # Clean up per-incident tracking when investigation is complete
    if state.status.value in ("report_ready", "needs_more_evidence"):
        _published_tool_call_ids.pop(incident_id, None)

    return _build_state_response(state, include_mode=str(getattr(_engine, "mode", "")))


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

    return _build_state_response(state, include_mode=str(getattr(_engine, "mode", "")))


@router.get("/{incident_id}/export")
async def export_investigation(incident_id: str) -> Response:
    """Export a versioned, redacted investigation payload for download.

    Returns a JSON response with Content-Disposition attachment header.
    The payload is sanitized to remove sensitive data (API keys, tokens,
    Authorization headers, etc.) and gated by a maximum size limit.
    """
    if _export_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Investigation export service not configured",
        )

    from incidentlens_control_plane.services.investigation_export import (
        InvestigationExportNotFound,
        InvestigationExportTooLarge,
    )

    try:
        payload = await _export_service.build_export(incident_id)
    except InvestigationExportNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InvestigationExportTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc

    filename = f"incidentlens-{incident_id}.json"
    return Response(
        content=json.dumps(payload),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
