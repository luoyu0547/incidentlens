"""Case memory API routes for the control plane.

Provides:
  - GET /api/cases/search — search for verified cases
  - POST /api/cases — save a new case
  - POST /api/cases/{case_id}/confirm — confirm a case as human_verified
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from incidentlens_control_plane.memory.repository import CaseSearchResult

router = APIRouter(prefix="/api/cases", tags=["cases"])

# Repository is set by main.py during app startup
_repository: Any = None


def set_repository(repository: Any) -> None:
    """Set the case repository for the routes."""
    global _repository
    _repository = repository


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class SaveCaseRequest(BaseModel):
    """Request body for saving a new case."""

    status: str = "pending_review"
    symptom: str = ""
    service: str = ""
    root_cause: str = ""
    resolution: str = ""
    evidence_summary: str = ""


class SaveCaseResponse(BaseModel):
    """Response for saving a case."""

    case_id: int


class CaseSearchResponse(BaseModel):
    """Response for case search."""

    results: list[CaseSearchResult]


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get("/search")
async def search_cases(
    keyword: str,
    service: str | None = None,
    root_cause: str | None = None,
) -> CaseSearchResponse:
    """Search for human_verified cases matching the keyword."""
    if _repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Case repository not configured",
        )

    results = _repository.search(keyword, service, root_cause)
    return CaseSearchResponse(results=results)


@router.post("", status_code=status.HTTP_201_CREATED)
async def save_case(request: SaveCaseRequest) -> SaveCaseResponse:
    """Save a new case to the case memory."""
    if _repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Case repository not configured",
        )

    case_id = _repository.save_case(
        status=request.status,
        symptom=request.symptom,
        service=request.service,
        root_cause=request.root_cause,
        resolution=request.resolution,
        evidence_summary=request.evidence_summary,
    )
    return SaveCaseResponse(case_id=case_id)


@router.post("/{case_id}/confirm")
async def confirm_case(case_id: int) -> dict[str, str]:
    """Confirm a case as human_verified."""
    if _repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Case repository not configured",
        )

    try:
        _repository.confirm(case_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )

    return {"status": "confirmed"}
