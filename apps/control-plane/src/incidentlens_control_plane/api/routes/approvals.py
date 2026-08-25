"""Authenticated product approval facade (``/api/v1/approvals``)."""

from __future__ import annotations

import base64
import json
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Annotated, ClassVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.api.errors import ApiProblem
from incidentlens_control_plane.api.idempotency import (
    execute_idempotent,
    idempotency_request_sha256,
)
from incidentlens_control_plane.api.models import ApiErrorResponse
from incidentlens_control_plane.approvals.service import (
    ApprovalAlreadyDecided,
    ApprovalExpired,
    ApprovalService,
)
from incidentlens_control_plane.approvals.store import ApprovalNotFound
from incidentlens_control_plane.approvals.types import (
    ApprovalDecisionStatus,
    ApprovalDownstreamStatus,
    ApprovalRecord,
    ApprovalStatus,
)
from incidentlens_control_plane.auth.dependencies import (
    authorize_target,
    get_principal,
    require_scopes,
)
from incidentlens_control_plane.auth.types import Principal, PrincipalScope
from incidentlens_control_plane.logs.redaction import redact_message

router = APIRouter(
    prefix="/api/v1/approvals",
    tags=["approvals"],
    dependencies=[Depends(get_principal)],
)

_APPROVE_ROUTE_KEY = "/api/v1/approvals/{approval_id}/approve"
_REJECT_ROUTE_KEY = "/api/v1/approvals/{approval_id}/reject"
_INLINE_SECRET_RE = re.compile(
    r"(?i)(?P<key>password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*(?P<value>[^\s\n]+)"
)
_RESUMABLE_DOWNSTREAM_STATUSES = frozenset(
    {
        ApprovalDownstreamStatus.PENDING,
        ApprovalDownstreamStatus.PROCESSING,
    }
)


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(min_length=1, max_length=1000)


class ApprovalDecisionConflictProblem(ApiProblem):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "approval_already_decided"
    message: ClassVar[str] = "approval was already decided"


class ApprovalExpiredProblem(ApiProblem):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "approval_expired"
    message: ClassVar[str] = "approval has expired"


class ApprovalLinkageView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str | None = None
    service: str | None = None
    session_id: str | None = None
    investigation_id: str | None = None
    agent_run_id: str | None = None
    tool_call_id: str | None = None
    changeset_id: str | None = None
    proposal_id: str | None = None


class ApprovalDetailView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=120)
    intent_summary: str = Field(min_length=1, max_length=1000)
    linkage: ApprovalLinkageView
    risk: str = Field(min_length=1, max_length=80)
    preview: str | None = Field(default=None, max_length=4000)
    diff: str | None = Field(default=None, max_length=4000)
    impact: str | None = Field(default=None, max_length=4000)
    verification: str | None = Field(default=None, max_length=4000)
    rollback: str | None = Field(default=None, max_length=4000)
    status: ApprovalStatus
    decision_status: ApprovalDecisionStatus
    downstream_status: ApprovalDownstreamStatus
    downstream_error_code: str | None = Field(default=None, max_length=120)
    decided_by: str | None = Field(default=None, max_length=200)
    decision_reason: str | None = Field(default=None, max_length=1000)
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    consumed_at: datetime | None = None
    downstream_updated_at: datetime | None = None


class ApprovalPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ApprovalDetailView, ...]
    next_cursor: str | None = None
    has_more: bool


def _error_response(status_code: int, description: str) -> dict[str, object]:
    return {"model": ApiErrorResponse, "description": description}


def _service(request: Request) -> ApprovalService:
    return request.app.state.runtime.approvals


def _encode_cursor(record: ApprovalRecord) -> str:
    raw = json.dumps(
        {
            "created_at": record.created_at.astimezone(UTC).isoformat(),
            "approval_id": record.approval_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "ap1_" + base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(value: str | None) -> tuple[datetime, str] | None:
    if value is None:
        return None
    if not value.startswith("ap1_"):
        raise ApiProblem(
            status_code=422,
            code="cursor_invalid",
            message="approval cursor is invalid",
        )
    try:
        payload = base64.urlsafe_b64decode(value[4:].encode("ascii")).decode("utf-8")
        body = json.loads(payload)
        created_at = datetime.fromisoformat(str(body["created_at"]))
        approval_id = str(body["approval_id"])
    except Exception as exc:  # noqa: BLE001
        raise ApiProblem(
            status_code=422,
            code="cursor_invalid",
            message="approval cursor is invalid",
        ) from exc
    return created_at, approval_id


def _bounded(value: object, *, limit: int = 4000) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = _INLINE_SECRET_RE.sub(_inline_secret_repl, value)
    return redact_message(normalized, max_length=limit).message_redacted


def _inline_secret_repl(match: re.Match[str]) -> str:
    key = match.group("key").lower()
    placeholder = (
        "[REDACTED_PASSWORD]"
        if key in {"password", "passwd", "pwd", "secret"}
        else "[REDACTED_TOKEN]"
    )
    return f"{match.group('key')}={placeholder}"


def _changeset_diff_summary(request: Request, changeset_id: str | None) -> str | None:
    if not changeset_id:
        return None
    changeset = request.app.state.runtime.change_store.get(changeset_id)
    if changeset is None or not changeset.files:
        return None

    scope_counts = Counter(file_change.scope for file_change in changeset.files)
    scope_summary = ", ".join(
        f"{count}×{scope}" for scope, count in sorted(scope_counts.items())
    )
    local_backups = sum(1 for file_change in changeset.files if file_change.local_backup_ref)
    remote_backups = sum(
        1 for file_change in changeset.files if file_change.remote_backup_path
    )
    checksum_pairs = sum(
        1
        for file_change in changeset.files
        if file_change.expected_sha256 and file_change.replacement_sha256
    )
    return (
        f"{len(changeset.files)} file change(s); scopes={scope_summary}; "
        f"local_backups={local_backups}; remote_backups={remote_backups}; "
        f"checksum_pairs={checksum_pairs}"
    )


def _detail_view(request: Request, record: ApprovalRecord) -> ApprovalDetailView:
    preview = dict(record.preview)
    derived_diff = _changeset_diff_summary(request, record.changeset_id)
    if record.changeset_id:
        changeset = request.app.state.runtime.change_store.get(record.changeset_id)
        if changeset is not None:
            if "verification" not in preview:
                preview["verification"] = changeset.verification_plan
            if "rollback" not in preview:
                preview["rollback"] = changeset.rollback_plan

    return ApprovalDetailView(
        approval_id=record.approval_id,
        kind=str(record.intent.get("kind", "unknown")),
        intent_summary=record.intent_summary,
        linkage=ApprovalLinkageView(
            target_id=record.target_id,
            service=record.service,
            session_id=record.session_id,
            investigation_id=record.investigation_id,
            agent_run_id=record.agent_run_id,
            tool_call_id=record.tool_call_id,
            changeset_id=record.changeset_id,
            proposal_id=record.proposal_id,
        ),
        risk=record.risk,
        preview=_bounded(preview.get("preview")) or _bounded(preview.get("summary")),
        diff=derived_diff,
        impact=_bounded(preview.get("impact")),
        verification=_bounded(preview.get("verification")),
        rollback=_bounded(preview.get("rollback")),
        status=record.status,
        decision_status=record.decision_status,
        downstream_status=record.downstream_status,
        downstream_error_code=record.downstream_error_code,
        decided_by=record.decision_actor,
        decision_reason=record.decision_reason,
        created_at=record.created_at,
        expires_at=record.expires_at,
        decided_at=record.decided_at,
        consumed_at=record.consumed_at,
        downstream_updated_at=record.downstream_updated_at,
    )


def _authorized(principal: Principal, record: ApprovalRecord) -> bool:
    target_id = record.target_id or ""
    return bool(target_id) and principal.authorized_for(target_id)


def _downstream_error_code(exc: Exception) -> str:
    if isinstance(exc, ApprovalNotFound):
        return "approval_not_found"
    if isinstance(exc, ApprovalExpired):
        return "approval_expired"
    if isinstance(exc, ApprovalAlreadyDecided):
        return "approval_already_decided"
    return "internal_error"


def _decision_matches_retry(
    record: ApprovalRecord,
    *,
    decision_status: ApprovalDecisionStatus,
    actor: str,
    reason: str,
    route_key: str,
    idempotency_key: str,
    request_sha256: str,
) -> bool:
    return (
        record.decision_status is decision_status
        and record.decision_actor == actor
        and record.decision_reason == reason
        and record.decision_route_key == route_key
        and record.decision_idempotency_key == idempotency_key
        and record.decision_request_sha256 == request_sha256
    )


async def _run_downstream(request: Request, record: ApprovalRecord) -> ApprovalRecord:
    approvals = _service(request)
    if not record.has_downstream_linkage:
        return approvals.mark_downstream(
            record.approval_id,
            ApprovalDownstreamStatus.NOT_APPLICABLE,
            now=datetime.now(UTC),
        )

    approvals.mark_downstream(
        record.approval_id,
        ApprovalDownstreamStatus.PROCESSING,
        now=datetime.now(UTC),
    )
    try:
        outcome = await request.app.state.runtime.investigations.handle_approval_decision(
            record.approval_id,
            now=datetime.now(UTC),
        )
    except Exception as exc:  # noqa: BLE001
        return approvals.mark_downstream(
            record.approval_id,
            ApprovalDownstreamStatus.FAILED,
            error_code=_downstream_error_code(exc),
            now=datetime.now(UTC),
        )

    final_status = (
        ApprovalDownstreamStatus.PROCESSED
        if outcome.matched != "none"
        else ApprovalDownstreamStatus.NOT_APPLICABLE
    )
    return approvals.mark_downstream(
        record.approval_id,
        final_status,
        now=datetime.now(UTC),
    )


async def _resume_matching_decision(
    request: Request,
    *,
    approval_id: str,
    decision_status: ApprovalDecisionStatus,
    actor: str,
    reason: str,
    route_key: str,
    idempotency_key: str,
    request_sha256: str,
) -> ApprovalRecord:
    record = _service(request).get(approval_id)
    if record is None:
        raise ApprovalNotFound(f"Approval '{approval_id}' not found")
    if not _decision_matches_retry(
        record,
        decision_status=decision_status,
        actor=actor,
        reason=reason,
        route_key=route_key,
        idempotency_key=idempotency_key,
        request_sha256=request_sha256,
    ):
        raise ApprovalAlreadyDecided(f"Approval '{approval_id}' is already decided")
    if record.downstream_status in _RESUMABLE_DOWNSTREAM_STATUSES:
        return await _run_downstream(request, record)
    return record


def _decision_problem(exc: Exception) -> Exception:
    if isinstance(exc, ApprovalNotFound):
        return HTTPException(status_code=404, detail="Approval not found")
    if isinstance(exc, ApprovalExpired):
        return ApprovalExpiredProblem()
    if isinstance(exc, ApprovalAlreadyDecided):
        return ApprovalDecisionConflictProblem()
    return exc


@router.get(
    "",
    response_model=ApprovalPage,
    operation_id="listApprovals",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
        422: _error_response(422, "Invalid approval cursor or filter"),
    },
)
async def list_approvals(
    request: Request,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
    status: ApprovalStatus | None = Query(default=None),
    target_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    investigation_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    after: str | None = Query(default=None),
) -> ApprovalPage:
    if target_id is not None:
        authorize_target(principal, target_id)
    cursor = _decode_cursor(after)
    items, has_more = _service(request).list_page(
        status=status,
        target_id=target_id,
        session_id=session_id,
        investigation_id=investigation_id,
        allowed_target_ids=principal.allowed_target_ids,
        limit=limit,
        after_created_at=cursor[0] if cursor else None,
        after_approval_id=cursor[1] if cursor else None,
    )
    next_cursor = _encode_cursor(items[-1]) if items and has_more else None
    return ApprovalPage(
        items=tuple(_detail_view(request, item) for item in items),
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/{approval_id}",
    response_model=ApprovalDetailView,
    operation_id="getApproval",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
        404: _error_response(404, "Approval not found"),
    },
)
async def get_approval(
    request: Request,
    approval_id: str,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
) -> ApprovalDetailView:
    record = _service(request).get(approval_id)
    if record is None or not _authorized(principal, record):
        raise HTTPException(status_code=404, detail="Approval not found")
    return _detail_view(request, record)


@router.post(
    "/{approval_id}/approve",
    response_model=ApprovalDetailView,
    operation_id="approveApproval",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
        404: _error_response(404, "Approval not found"),
        409: _error_response(
            409,
            "Approval expired, already decided, or the Idempotency-Key conflicts",
        ),
        422: _error_response(422, "Validation failed or Idempotency-Key missing"),
    },
)
async def approve_approval(
    request: Request,
    response: Response,
    approval_id: str,
    body: ApprovalDecisionRequest,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.APPROVE))],
) -> ApprovalDetailView:
    existing = _service(request).get(approval_id)
    if existing is None or not _authorized(principal, existing):
        raise HTTPException(status_code=404, detail="Approval not found")

    canonical_body = json.dumps(
        body.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    request_sha256 = idempotency_request_sha256(
        method="POST",
        route_key=_APPROVE_ROUTE_KEY,
        path_params={"approval_id": approval_id},
        canonical_body=canonical_body,
    )

    async def action() -> tuple[int, ApprovalDetailView]:
        resumed_retry = False
        try:
            decided = await _service(request).approve(
                approval_id,
                now=datetime.now(UTC),
                actor=principal.principal_id,
                reason=body.reason,
                route_key=_APPROVE_ROUTE_KEY,
                idempotency_key=request.headers.get("Idempotency-Key", ""),
                request_sha256=request_sha256,
            )
        except ApprovalAlreadyDecided:
            try:
                decided = await _resume_matching_decision(
                    request,
                    approval_id=approval_id,
                    decision_status=ApprovalDecisionStatus.APPROVED,
                    actor=principal.principal_id,
                    reason=body.reason,
                    route_key=_APPROVE_ROUTE_KEY,
                    idempotency_key=request.headers.get("Idempotency-Key", ""),
                    request_sha256=request_sha256,
                )
                resumed_retry = True
            except (ApprovalNotFound, ApprovalExpired, ApprovalAlreadyDecided) as exc:
                raise _decision_problem(exc) from exc
        except (ApprovalNotFound, ApprovalExpired) as exc:
            raise _decision_problem(exc) from exc
        if not resumed_retry:
            decided = await _run_downstream(request, decided)
        return 200, _detail_view(request, decided)

    status_code, payload, replayed = await execute_idempotent(
        service=request.app.state.runtime.idempotency,
        principal=principal,
        method="POST",
        route_key=_APPROVE_ROUTE_KEY,
        idempotency_key=request.headers.get("Idempotency-Key", ""),
        request_sha256=request_sha256,
        response_type=ApprovalDetailView,
        action=action,
    )
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    response.status_code = status_code
    return payload


@router.post(
    "/{approval_id}/reject",
    response_model=ApprovalDetailView,
    operation_id="rejectApproval",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
        404: _error_response(404, "Approval not found"),
        409: _error_response(
            409,
            "Approval expired, already decided, or the Idempotency-Key conflicts",
        ),
        422: _error_response(422, "Validation failed or Idempotency-Key missing"),
    },
)
async def reject_approval(
    request: Request,
    response: Response,
    approval_id: str,
    body: ApprovalDecisionRequest,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.APPROVE))],
) -> ApprovalDetailView:
    existing = _service(request).get(approval_id)
    if existing is None or not _authorized(principal, existing):
        raise HTTPException(status_code=404, detail="Approval not found")

    canonical_body = json.dumps(
        body.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    request_sha256 = idempotency_request_sha256(
        method="POST",
        route_key=_REJECT_ROUTE_KEY,
        path_params={"approval_id": approval_id},
        canonical_body=canonical_body,
    )

    async def action() -> tuple[int, ApprovalDetailView]:
        resumed_retry = False
        try:
            decided = await _service(request).reject(
                approval_id,
                now=datetime.now(UTC),
                actor=principal.principal_id,
                reason=body.reason,
                route_key=_REJECT_ROUTE_KEY,
                idempotency_key=request.headers.get("Idempotency-Key", ""),
                request_sha256=request_sha256,
            )
        except ApprovalAlreadyDecided:
            try:
                decided = await _resume_matching_decision(
                    request,
                    approval_id=approval_id,
                    decision_status=ApprovalDecisionStatus.REJECTED,
                    actor=principal.principal_id,
                    reason=body.reason,
                    route_key=_REJECT_ROUTE_KEY,
                    idempotency_key=request.headers.get("Idempotency-Key", ""),
                    request_sha256=request_sha256,
                )
                resumed_retry = True
            except (ApprovalNotFound, ApprovalExpired, ApprovalAlreadyDecided) as exc:
                raise _decision_problem(exc) from exc
        except (ApprovalNotFound, ApprovalExpired) as exc:
            raise _decision_problem(exc) from exc
        if not resumed_retry:
            decided = await _run_downstream(request, decided)
        return 200, _detail_view(request, decided)

    status_code, payload, replayed = await execute_idempotent(
        service=request.app.state.runtime.idempotency,
        principal=principal,
        method="POST",
        route_key=_REJECT_ROUTE_KEY,
        idempotency_key=request.headers.get("Idempotency-Key", ""),
        request_sha256=request_sha256,
        response_type=ApprovalDetailView,
        action=action,
    )
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    response.status_code = status_code
    return payload


__all__ = [
    "ApprovalDecisionRequest",
    "ApprovalDetailView",
    "ApprovalDownstreamStatus",
    "ApprovalPage",
    "router",
]
