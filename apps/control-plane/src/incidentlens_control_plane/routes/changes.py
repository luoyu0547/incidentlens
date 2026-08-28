"""ChangeSet inspection, verification, and rollback HTTP API routes.

Views are dedicated schemas so backup content is never serialized: the domain
stores only opaque backup references, and the API returns those references plus
diff/validation/rollback status without any plaintext.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.changes.manager import ChangeVerifyError
from incidentlens_control_plane.changes.types import (
    ChangeSet,
    ChangeSetStatus,
    FileChange,
)
from incidentlens_control_plane.operations.state_machine import OPERATION_TERMINAL
from incidentlens_control_plane.operations.types import OperationKind
from incidentlens_control_plane.routes import get_runtime
from incidentlens_control_plane.runtime import RuntimeServices

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/changes", tags=["changes"])


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result: str = Field(min_length=1, max_length=100)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str | None = Field(default=None, min_length=1, max_length=120)


class FileChangeView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file_change_id: str
    scope: str
    remote_path: str
    expected_sha256: str | None
    replacement_sha256: str
    diff_text: str
    local_backup_ref: str | None
    remote_backup_path: str
    temp_path: str | None
    applied: bool
    validation_result: str | None
    rollback_result: str | None


class ChangeSetView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    changeset_id: str
    incident_id: str
    project_id: str
    target_id: str
    service_name: str
    status: ChangeSetStatus
    created_at: datetime | None
    updated_at: datetime | None
    verification_plan: str
    rollback_plan: str
    approval_id: str | None
    files: tuple[FileChangeView, ...]


def _to_view(changeset: ChangeSet) -> ChangeSetView:
    return ChangeSetView(
        changeset_id=changeset.changeset_id,
        incident_id=changeset.incident_id,
        project_id=changeset.project_id,
        target_id=changeset.target_id,
        service_name=changeset.service_name,
        status=changeset.status,
        created_at=changeset.created_at,
        updated_at=changeset.updated_at,
        verification_plan=changeset.verification_plan,
        rollback_plan=changeset.rollback_plan,
        approval_id=changeset.approval_id,
        files=tuple(_file_change_view(file_change) for file_change in changeset.files),
    )


def _file_change_view(file_change: FileChange) -> FileChangeView:
    return FileChangeView(
        file_change_id=file_change.file_change_id,
        scope=file_change.scope,
        remote_path=file_change.remote_path,
        expected_sha256=file_change.expected_sha256,
        replacement_sha256=file_change.replacement_sha256,
        diff_text=file_change.diff_text,
        local_backup_ref=file_change.local_backup_ref,
        remote_backup_path=file_change.remote_backup_path,
        temp_path=file_change.temp_path,
        applied=file_change.applied,
        validation_result=file_change.validation_result,
        rollback_result=file_change.rollback_result,
    )


@router.get("/{changeset_id}")
async def get_changeset(request: Request, changeset_id: str) -> dict[str, Any]:
    """Return a ChangeSet view with backup references but never plaintext."""
    runtime = get_runtime(request)
    changeset = runtime.change_store.get(changeset_id)
    if changeset is None:
        raise HTTPException(status_code=404, detail="ChangeSet not found")
    return _to_view(changeset).model_dump(mode="json")


@router.post("/{changeset_id}/verify")
async def verify_changeset(
    request: Request, changeset_id: str, body: VerifyRequest
) -> dict[str, Any]:
    """Record a structured verification result and move the state machine."""
    runtime = get_runtime(request)
    try:
        await runtime.changes.verify(changeset_id, body.result)
    except ChangeVerifyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    changeset = runtime.change_store.get(changeset_id)
    return _to_view(changeset).model_dump(mode="json")


async def _await_operation_terminal(
    runtime: RuntimeServices, operation_id: str, *, timeout: float = 30.0
) -> None:
    """Wait (bounded) for a durable operation to reach a terminal status.

    The legacy rollback contract ran the restore in a ``BackgroundTask`` before
    the request returned, so callers observed the rollback complete shortly
    after the 202.  The durable path keeps the same visible timing by waiting
    for the operation dispatcher to finalise the operation before handing back
    the 202 body.  A timeout only logs and returns -- the route still answers
    202 with the operation running in the background.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        operation = runtime.operation_service.get_operation(operation_id)
        if operation.status in OPERATION_TERMINAL:
            return
        if loop.time() >= deadline:
            logger.warning(
                "operation %s did not reach a terminal status within %.1fs",
                operation_id,
                timeout,
            )
            return
        await asyncio.sleep(0.05)


@router.post("/{changeset_id}/rollback", status_code=202)
async def rollback_changeset(
    request: Request,
    changeset_id: str,
    body: RollbackRequest | None = None,
) -> dict[str, str]:
    """Roll a ChangeSet back; returns 202 while the restore executes.

    The rollback is enqueued as a durable ``ROLLBACK`` operation and dispatched
    by the worker loop, so a restart can never silently re-run it.  The response
    body is preserved byte-for-byte from the legacy contract.
    """
    runtime = get_runtime(request)
    changeset = runtime.change_store.get(changeset_id)
    if changeset is None:
        raise HTTPException(status_code=404, detail="ChangeSet not found")
    if changeset.status not in (ChangeSetStatus.APPLIED, ChangeSetStatus.VALIDATED):
        raise HTTPException(
            status_code=409,
            detail=f"cannot roll back changeset in status {changeset.status.value}",
        )

    approval_id = body.approval_id if body is not None else None
    if runtime.changes.interrupts_service(changeset) and approval_id is None:
        raise HTTPException(
            status_code=409,
            detail="an approval is required to roll back a service-interrupting changeset",
        )

    operation = runtime.operation_service.enqueue(
        kind=OperationKind.ROLLBACK,
        target_id=changeset.target_id,
        created_by="legacy-api",
        request_payload=json.dumps(
            {"changeset_id": changeset_id, "approval_id": approval_id},
            sort_keys=True,
            separators=(",", ":"),
        ),
        now=datetime.now(UTC),
    )
    await _await_operation_terminal(runtime, operation.operation_id)
    return {"changeset_id": changeset_id, "status": "rolling_back"}
