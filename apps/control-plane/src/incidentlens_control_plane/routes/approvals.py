"""Approval decision HTTP API routes.

Responses use a dedicated view schema so the canonical intent mapping is never
serialized; clients receive the redacted summary and lifecycle timestamps only.
A successful approve/reject also asks ``InvestigationService`` to resolve any
matching tool call or registry proposal (re-executing an approved tool,
rejecting a denied one, applying or refusing a registry update, and resuming
the parked run).  That linkage consumes the exact single-use intent and is
best-effort: the decision itself is already recorded, so a linkage failure is
logged, never surfaced as a failed approval.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.approvals.service import (
    ApprovalAlreadyDecided,
    ApprovalExpired,
)
from incidentlens_control_plane.approvals.store import ApprovalNotFound
from incidentlens_control_plane.approvals.types import ApprovalRecord, ApprovalStatus
from incidentlens_control_plane.routes import get_runtime
from incidentlens_control_plane.runtime import RuntimeServices

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class ApprovalView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str = Field(min_length=1, max_length=120)
    intent_summary: str = Field(min_length=1, max_length=1000)
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    consumed_at: datetime | None


def _to_view(record: ApprovalRecord) -> ApprovalView:
    return ApprovalView(
        approval_id=record.approval_id,
        intent_summary=record.intent_summary,
        status=record.status,
        created_at=record.created_at,
        expires_at=record.expires_at,
        decided_at=record.decided_at,
        consumed_at=record.consumed_at,
    )


async def _link_investigation_decision(
    runtime: RuntimeServices, approval_id: str
) -> None:
    """Resolve an investigation tool/proposal blocked on ``approval_id``.

    Best-effort: the approval is already decided; a failed linkage must never
    make the decision endpoint fail.  The exact single-use intent is consumed
    by the underlying gateway / proposal service.
    """
    try:
        await runtime.investigations.handle_approval_decision(approval_id)
    except Exception:  # noqa: BLE001 - linkage is best-effort
        logger.exception(
            "investigation approval linkage failed for approval %s", approval_id
        )


@router.get("")
async def list_approvals(
    request: Request,
    status: ApprovalStatus | None = Query(default=None),
) -> list[dict[str, Any]]:
    """List approval records, optionally filtered by status."""
    runtime = get_runtime(request)
    records = runtime.approvals.list(status)
    return [_to_view(record).model_dump(mode="json") for record in records]


@router.post("/{approval_id}/approve")
async def approve_approval(request: Request, approval_id: str) -> dict[str, Any]:
    """Approve a pending approval request. Single-use; repeats return 409."""
    runtime = get_runtime(request)
    try:
        record = await runtime.approvals.approve(approval_id)
    except (ApprovalNotFound, ApprovalExpired, ApprovalAlreadyDecided):
        raise HTTPException(
            status_code=409, detail="Approval not found or already decided"
        )
    await _link_investigation_decision(runtime, approval_id)
    return _to_view(record).model_dump(mode="json")


@router.post("/{approval_id}/reject")
async def reject_approval(request: Request, approval_id: str) -> dict[str, Any]:
    """Reject a pending approval request. Single-use; repeats return 409."""
    runtime = get_runtime(request)
    try:
        record = await runtime.approvals.reject(approval_id)
    except (ApprovalNotFound, ApprovalExpired, ApprovalAlreadyDecided):
        raise HTTPException(
            status_code=409, detail="Approval not found or already decided"
        )
    await _link_investigation_decision(runtime, approval_id)
    return _to_view(record).model_dump(mode="json")
