"""Case memory API routes for the control plane — governed lifecycle.

Provides:
  - POST /api/cases — create a draft case (never allows client-selected status)
  - GET /api/cases — list cases with filtering and cursor pagination
  - GET /api/cases/search — hybrid search for cases with mode/score explanation
  - GET /api/cases/{case_id} — get a single case
  - PATCH /api/cases/{case_id} — edit a case (returns to draft)
  - POST /api/cases/{case_id}/confirm — confirm as human_verified
  - POST /api/cases/{case_id}/reject — reject a case
  - POST /api/cases/{case_id}/deprecate — deprecate a verified case
  - POST /api/cases/{case_id}/feedback — record search feedback
  - GET /api/cases/{case_id}/history — get case review history
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from incidentlens_control_plane.memory.domain import (
    CaseSearchHit,
    CaseSearchQuery,
    CaseSnapshot,
    CaseStatus,
    FeedbackCommand,
    FeedbackRating,
)
from incidentlens_control_plane.memory.service import (
    CaseConflictError,
    CaseNotFoundError,
    CaseService,
    CaseValidationError,
    InvalidCaseTransitionError,
)

router = APIRouter(prefix="/api/cases", tags=["cases"])

# ---------------------------------------------------------------------------
# Injected dependencies — set by main.py or test fixtures
# ---------------------------------------------------------------------------

_case_service: CaseService | None = None
_retriever: Any = None
_repository: Any = None


def set_case_service(service: CaseService) -> None:
    """Set the case service for the routes."""
    global _case_service  # noqa: PLW0603
    _case_service = service


def set_retriever(retriever: Any) -> None:
    """Set the hybrid case retriever for the routes."""
    global _retriever  # noqa: PLW0603
    _retriever = retriever


def set_repository(repository: Any) -> None:
    """Set the case repository for the routes (legacy fallback)."""
    global _repository  # noqa: PLW0603
    _repository = repository


def _get_service() -> CaseService:
    """Return the case service or raise 503."""
    if _case_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Case service not configured",
        )
    return _case_service


def _get_retriever() -> Any:
    """Return the hybrid retriever or raise 503."""
    if _retriever is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Case retriever not configured",
        )
    return _retriever


# ---------------------------------------------------------------------------
# Request models (no status field — clients cannot write target status)
# ---------------------------------------------------------------------------


class CreateDraftRequest(BaseModel):
    """Request body for creating a draft case.

    The ``status`` field is intentionally absent: ``POST /api/cases`` always
    produces a ``draft`` — clients cannot choose the target status.
    """

    model_config = {"extra": "forbid"}

    symptom: str = Field(min_length=1, max_length=4000)
    affected_services: list[str] = Field(min_length=1, max_length=20)
    actor: str = Field(min_length=1, max_length=255)
    root_cause_category: str = Field(default="", max_length=255)
    root_cause_description: str = Field(default="", max_length=8000)
    key_evidence: list[dict[str, Any]] = Field(default_factory=list)
    investigation_path: list[dict[str, Any]] = Field(default_factory=list)
    invalid_hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    resolution: str = Field(default="", max_length=8000)
    remediation_advice: list[str] = Field(default_factory=list)
    applicability_conditions: list[str] = Field(default_factory=list)
    inapplicability_conditions: list[str] = Field(default_factory=list)
    environment: str = Field(default="", max_length=255)
    service_version_exact: str = Field(default="", max_length=255)
    service_version_min: str = Field(default="", max_length=255)
    service_version_max: str = Field(default="", max_length=255)


class EditCaseRequest(BaseModel):
    """Request body for editing a case via PATCH.

    Includes ``expected_version`` for optimistic locking. Editing always
    moves the case back to ``draft`` regardless of its current status.
    """

    expected_version: int
    actor: str = Field(min_length=1, max_length=255)
    reason: str = Field(default="", max_length=4000)
    symptom: str = Field(min_length=1, max_length=4000)
    affected_services: list[str] = Field(min_length=1, max_length=20)
    root_cause_category: str = Field(default="", max_length=255)
    root_cause_description: str = Field(default="", max_length=8000)
    key_evidence: list[dict[str, Any]] = Field(default_factory=list)
    investigation_path: list[dict[str, Any]] = Field(default_factory=list)
    invalid_hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    resolution: str = Field(default="", max_length=8000)
    remediation_advice: list[str] = Field(default_factory=list)
    applicability_conditions: list[str] = Field(default_factory=list)
    inapplicability_conditions: list[str] = Field(default_factory=list)
    environment: str = Field(default="", max_length=255)
    service_version_exact: str = Field(default="", max_length=255)
    service_version_min: str = Field(default="", max_length=255)
    service_version_max: str = Field(default="", max_length=255)


class ReviewRequest(BaseModel):
    """Request body for confirm / reject / deprecate actions."""

    expected_version: int
    actor: str = Field(min_length=1, max_length=255)
    reason: str = Field(default="", max_length=4000)


class FeedbackRequest(BaseModel):
    """Request body for recording search feedback."""

    idempotency_key: str = Field(min_length=1, max_length=255)
    rating: FeedbackRating
    incident_id: str | None = Field(default=None, max_length=255)
    actor: str = Field(min_length=1, max_length=255)
    comment: str = Field(default="", max_length=4000)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CaseResponse(BaseModel):
    """Full case snapshot returned by case governance endpoints."""

    id: int
    revision: int
    status: CaseStatus
    incident_id: str | None = None
    source_reference: str = ""
    symptom: str
    affected_services: list[str]
    root_cause_category: str = ""
    root_cause_description: str = ""
    key_evidence: list[dict[str, Any]] = Field(default_factory=list)
    investigation_path: list[dict[str, Any]] = Field(default_factory=list)
    invalid_hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    resolution: str = ""
    remediation_advice: list[str] = Field(default_factory=list)
    applicability_conditions: list[str] = Field(default_factory=list)
    inapplicability_conditions: list[str] = Field(default_factory=list)
    environment: str = ""
    service_version_exact: str = ""
    service_version_min: str = ""
    service_version_max: str = ""
    source_report_json: str = ""
    created_at: str
    updated_at: str

    model_config = {"json_schema_extra": {"examples": []}}


class CaseListResponse(BaseModel):
    """Paginated list of cases."""

    cases: list[CaseResponse]
    next_cursor: int | None = None


class CaseSearchResponse(BaseModel):
    """Hybrid search results with mode, scores, and explanation."""

    results: list[CaseSearchHit]


class CaseHistoryResponse(BaseModel):
    """Append-only review action history for a case."""

    reviews: list[dict[str, Any]]
    feedback: list[dict[str, Any]]
    usage_events: list[dict[str, Any]]


class FeedbackResponse(BaseModel):
    """Response after recording feedback."""

    id: int
    case_id: int
    idempotency_key: str
    rating: FeedbackRating
    incident_id: str | None = None
    actor: str
    comment: str = ""
    created_at: str


# ---------------------------------------------------------------------------
# Helper: domain -> response mapping
# ---------------------------------------------------------------------------


def _snapshot_to_response(snap: CaseSnapshot) -> CaseResponse:
    """Convert a CaseSnapshot read model to a CaseResponse."""
    return CaseResponse(
        id=snap.id,
        revision=snap.revision,
        status=snap.status,
        incident_id=snap.incident_id,
        source_reference=snap.source_reference,
        symptom=snap.symptom,
        affected_services=snap.affected_services,
        root_cause_category=snap.root_cause_category,
        root_cause_description=snap.root_cause_description,
        key_evidence=snap.key_evidence,
        investigation_path=snap.investigation_path,
        invalid_hypotheses=snap.invalid_hypotheses,
        resolution=snap.resolution,
        remediation_advice=snap.remediation_advice,
        applicability_conditions=snap.applicability_conditions,
        inapplicability_conditions=snap.inapplicability_conditions,
        environment=snap.environment,
        service_version_exact=snap.service_version_exact,
        service_version_min=snap.service_version_min,
        service_version_max=snap.service_version_max,
        source_report_json=snap.source_report_json,
        created_at=snap.created_at.isoformat(),
        updated_at=snap.updated_at.isoformat(),
    )


def _request_to_draft(req: CreateDraftRequest) -> Any:
    """Convert a request model to a CaseDraft domain command."""
    from incidentlens_control_plane.memory.domain import CaseDraft

    return CaseDraft(
        symptom=req.symptom,
        affected_services=req.affected_services,
        root_cause_category=req.root_cause_category,
        root_cause_description=req.root_cause_description,
        key_evidence=req.key_evidence,
        investigation_path=req.investigation_path,
        invalid_hypotheses=req.invalid_hypotheses,
        resolution=req.resolution,
        remediation_advice=req.remediation_advice,
        applicability_conditions=req.applicability_conditions,
        inapplicability_conditions=req.inapplicability_conditions,
        environment=req.environment,
        service_version_exact=req.service_version_exact,
        service_version_min=req.service_version_min,
        service_version_max=req.service_version_max,
    )


def _edit_request_to_draft(req: EditCaseRequest) -> Any:
    """Convert an edit request to a CaseDraft domain command."""
    from incidentlens_control_plane.memory.domain import CaseDraft

    return CaseDraft(
        symptom=req.symptom,
        affected_services=req.affected_services,
        root_cause_category=req.root_cause_category,
        root_cause_description=req.root_cause_description,
        key_evidence=req.key_evidence,
        investigation_path=req.investigation_path,
        invalid_hypotheses=req.invalid_hypotheses,
        resolution=req.resolution,
        remediation_advice=req.remediation_advice,
        applicability_conditions=req.applicability_conditions,
        inapplicability_conditions=req.inapplicability_conditions,
        environment=req.environment,
        service_version_exact=req.service_version_exact,
        service_version_min=req.service_version_min,
        service_version_max=req.service_version_max,
    )


def _map_service_error(exc: Exception) -> HTTPException:
    """Map domain service errors to HTTP status codes."""
    if isinstance(exc, CaseNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (CaseConflictError, InvalidCaseTransitionError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, CaseValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc  # pragma: no cover


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.post("", response_model=CaseResponse, status_code=status.HTTP_201_CREATED)
async def create_case(request: CreateDraftRequest) -> CaseResponse:
    """Create a new draft case.

    Always produces status=draft. Clients cannot set a target status directly.
    """
    svc = _get_service()
    try:
        snapshot = svc.create_draft(
            _request_to_draft(request),
            actor=request.actor,
        )
    except Exception as exc:
        raise _map_service_error(exc) from exc
    return _snapshot_to_response(snapshot)


@router.get("", response_model=CaseListResponse)
async def list_cases(
    case_status: CaseStatus | None = Query(default=None, alias="status"),
    incident_id: str | None = None,
    service: str | None = Query(default=None, alias="service"),
    root_cause_category: str | None = None,
    environment: str | None = None,
    service_version: str | None = None,
    cursor: int | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> CaseListResponse:
    """List cases with optional filtering and cursor-based pagination."""
    svc = _get_service()
    if svc.repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Case repository not configured",
        )

    from incidentlens_control_plane.memory.models import CaseRow
    from incidentlens_control_plane.memory.retrieval import _row_to_snapshot

    with svc.repo.transaction() as session:
        query = session.query(CaseRow)
        if case_status is not None:
            query = query.filter(CaseRow.status == case_status.value)
        if incident_id is not None:
            query = query.filter(CaseRow.incident_id == incident_id)
        if service is not None:
            query = query.filter(
                CaseRow.affected_services_json.contains(f'"{service}"')
            )
        if root_cause_category is not None:
            query = query.filter(CaseRow.root_cause_category == root_cause_category)
        if environment is not None:
            query = query.filter(CaseRow.environment == environment)

        query = query.order_by(CaseRow.id)
        if cursor is not None:
            query = query.filter(CaseRow.id > cursor)

        rows = query.limit(limit + 1).all()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        cases = [_snapshot_to_response(_row_to_snapshot(row)) for row in page_rows]
        next_cursor = page_rows[-1].id if has_more and page_rows else None

    return CaseListResponse(cases=cases, next_cursor=next_cursor)


@router.get("/search", response_model=CaseSearchResponse)
async def search_cases(
    q: str = Query(min_length=1, max_length=2000),
    service: str | None = None,
    root_cause_category: str | None = None,
    environment: str | None = None,
    service_version: str | None = None,
    limit: int = Query(default=10, ge=1, le=20),
) -> CaseSearchResponse:
    """Search for cases using hybrid retrieval (FTS5 + optional semantic).

    Returns results with retrieval_mode, lexical_score, semantic_score,
    and similarity_reason for explainability.
    """
    retriever = _get_retriever()
    query = CaseSearchQuery(
        text=q,
        service=service,
        root_cause_category=root_cause_category,
        environment=environment,
        service_version=service_version,
        limit=limit,
    )
    hits = retriever.search(query)
    return CaseSearchResponse(results=hits)


@router.get("/{case_id}", response_model=CaseResponse)
async def get_case(case_id: int) -> CaseResponse:
    """Get a single case by ID."""
    svc = _get_service()
    if svc.repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Case repository not configured",
        )
    from incidentlens_control_plane.memory.models import CaseRow
    from incidentlens_control_plane.memory.retrieval import _row_to_snapshot

    with svc.repo.transaction() as session:
        row = session.get(CaseRow, case_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"case {case_id} not found")
        snapshot = _row_to_snapshot(row)
    return _snapshot_to_response(snapshot)


@router.patch("/{case_id}", response_model=CaseResponse)
async def edit_case(case_id: int, request: EditCaseRequest) -> CaseResponse:
    """Edit a case. Always moves the case back to draft status."""
    svc = _get_service()
    try:
        snapshot = svc.edit(
            case_id,
            expected_version=request.expected_version,
            patch=_edit_request_to_draft(request),
            actor=request.actor,
            reason=request.reason,
        )
    except Exception as exc:
        raise _map_service_error(exc) from exc
    return _snapshot_to_response(snapshot)


@router.post("/{case_id}/confirm", response_model=CaseResponse)
async def confirm_case(case_id: int, request: ReviewRequest) -> CaseResponse:
    """Confirm a draft or agent_generated case as human_verified."""
    svc = _get_service()
    try:
        snapshot = svc.confirm(
            case_id,
            expected_version=request.expected_version,
            actor=request.actor,
            reason=request.reason,
        )
    except Exception as exc:
        raise _map_service_error(exc) from exc
    return _snapshot_to_response(snapshot)


@router.post("/{case_id}/reject", response_model=CaseResponse)
async def reject_case(case_id: int, request: ReviewRequest) -> CaseResponse:
    """Reject a draft or agent_generated case."""
    svc = _get_service()
    try:
        snapshot = svc.reject(
            case_id,
            expected_version=request.expected_version,
            actor=request.actor,
            reason=request.reason,
        )
    except Exception as exc:
        raise _map_service_error(exc) from exc
    return _snapshot_to_response(snapshot)


@router.post("/{case_id}/deprecate", response_model=CaseResponse)
async def deprecate_case(case_id: int, request: ReviewRequest) -> CaseResponse:
    """Deprecate a human_verified case."""
    svc = _get_service()
    try:
        snapshot = svc.deprecate(
            case_id,
            expected_version=request.expected_version,
            actor=request.actor,
            reason=request.reason,
        )
    except Exception as exc:
        raise _map_service_error(exc) from exc
    return _snapshot_to_response(snapshot)


@router.post("/{case_id}/feedback", status_code=status.HTTP_201_CREATED)
async def add_feedback(
    case_id: int, request: FeedbackRequest
) -> FeedbackResponse:
    """Record feedback on a case search result (idempotent by key)."""
    svc = _get_service()
    try:
        record = svc.add_feedback(
            FeedbackCommand(
                case_id=case_id,
                idempotency_key=request.idempotency_key,
                rating=request.rating,
                incident_id=request.incident_id,
                actor=request.actor,
                comment=request.comment,
            )
        )
    except CaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CaseValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FeedbackResponse(
        id=record.id,
        case_id=record.case_id,
        idempotency_key=record.idempotency_key,
        rating=record.rating,
        incident_id=record.incident_id,
        actor=record.actor,
        comment=record.comment,
        created_at=record.created_at.isoformat(),
    )


@router.get("/{case_id}/history", response_model=CaseHistoryResponse)
async def case_history(case_id: int) -> CaseHistoryResponse:
    """Get the append-only review action history for a case."""
    svc = _get_service()
    if svc.repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Case repository not configured",
        )
    from incidentlens_control_plane.memory.models import (
        CaseFeedbackRow,
        CaseReviewActionRow,
        CaseRow,
        CaseUsageEventRow,
    )

    with svc.repo.transaction() as session:
        row = session.get(CaseRow, case_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"case {case_id} not found"
            )
        reviews = (
            session.query(CaseReviewActionRow)
            .filter(CaseReviewActionRow.case_id == case_id)
            .order_by(CaseReviewActionRow.id)
            .all()
        )
        review_data = [
            {
                "id": r.id,
                "case_id": r.case_id,
                "action": r.action,
                "actor": r.actor,
                "reason": r.reason,
                "previous_status": r.previous_status,
                "new_status": r.new_status,
                "created_at": r.created_at.isoformat(),
            }
            for r in reviews
        ]
        feedback_rows = (
            session.query(CaseFeedbackRow)
            .filter(CaseFeedbackRow.case_id == case_id)
            .order_by(CaseFeedbackRow.id)
            .all()
        )
        feedback_data = [
            {
                "id": item.id,
                "case_id": item.case_id,
                "incident_id": item.incident_id,
                "actor": item.actor,
                "rating": item.rating,
                "comment": item.comment,
                "idempotency_key": item.idempotency_key,
                "created_at": item.created_at.isoformat(),
            }
            for item in feedback_rows
        ]
        usage_rows = (
            session.query(CaseUsageEventRow)
            .filter(CaseUsageEventRow.case_id == case_id)
            .order_by(CaseUsageEventRow.id)
            .all()
        )
        usage_data = [
            {
                "id": item.id,
                "case_id": item.case_id,
                "incident_id": item.investigation_id,
                "hypothesis_id": item.hypothesis_id,
                "event_type": item.event_type,
                "idempotency_key": item.idempotency_key,
                "details": json.loads(item.details_json) if item.details_json else {},
                "created_at": item.created_at.isoformat(),
            }
            for item in usage_rows
        ]
    return CaseHistoryResponse(
        reviews=review_data,
        feedback=feedback_data,
        usage_events=usage_data,
    )
