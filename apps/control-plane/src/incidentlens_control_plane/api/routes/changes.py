"""Durable operation routes for ChangeSet rollback (``/api/v1/changesets``).

``POST /api/v1/changesets/{id}/rollback`` validates the ChangeSet is in a
rollback-able state (and so is ``interrupts_service``), then enqueues a durable
``ROLLBACK`` operation under the idempotency machinery instead of executing the
restore inline.  The route answers ``202 OperationAccepted`` immediately; the
operation dispatcher owns the actual restore through the registered rollback
handler, so a restart can never replay an unconfirmed rollback.

All preconditions are checked inside the idempotent ``action`` so a replayed
same-key request returns the pinned ``OperationAccepted`` regardless of how the
changeset state changed after the original success.

Every endpoint is protected by :func:`get_principal`, requires the ``OPERATE``
scope for the mutation, and documents its ``ApiErrorResponse`` failure cases so
the stable v1 envelope is part of the OpenAPI contract.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.api.idempotency import (
    execute_idempotent,
    idempotency_request_sha256,
)
from incidentlens_control_plane.api.models import ApiErrorResponse
from incidentlens_control_plane.auth.dependencies import get_principal, require_scopes
from incidentlens_control_plane.auth.types import Principal, PrincipalScope
from incidentlens_control_plane.changes.types import ChangeSetStatus
from incidentlens_control_plane.operations.types import OperationAccepted, OperationKind

router = APIRouter(
    prefix="/api/v1/changesets",
    tags=["changesets"],
    dependencies=[Depends(get_principal)],
)

_ROLLBACK_ROUTE_KEY = "/api/v1/changesets/{changeset_id}/rollback"

_ROLLBACKABLE_STATUSES = frozenset(
    {ChangeSetStatus.APPLIED, ChangeSetStatus.VALIDATED}
)


class RollbackRequest(BaseModel):
    """Body for ``POST /api/v1/changesets/{id}/rollback``.

    ``approval_id`` is the single-use approval covering a service-interrupting
    rollback; it is carried into the durable operation payload so the rollback
    handler can consume it exactly once.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str | None = Field(default=None, min_length=1, max_length=120)


class ChangeSetNotFoundError(Exception):
    """The addressed changeset has no persisted row."""


class ChangeSetNotRollbackableError(Exception):
    """The changeset is in a state that cannot be rolled back."""


class RollbackRequiresApprovalError(Exception):
    """A service-interrupting rollback was requested without an approval."""


def _error_response(status_code: int, description: str) -> dict[str, object]:
    """Build an OpenAPI ``responses`` entry that carries the v1 error envelope."""
    return {"model": ApiErrorResponse, "description": description}


def _to_http(exc: BaseException) -> Exception:
    """Map the route's domain failures to the stable v1 HTTP envelope."""
    if isinstance(exc, ChangeSetNotFoundError):
        return HTTPException(status_code=404, detail="ChangeSet not found")
    if isinstance(exc, ChangeSetNotRollbackableError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RollbackRequiresApprovalError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="Internal server error")


@router.post(
    "/{changeset_id}/rollback",
    status_code=202,
    response_model=OperationAccepted,
    operation_id="rollbackChangeset",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
        404: _error_response(404, "ChangeSet not found"),
        409: _error_response(
            409,
            "ChangeSet is not rollback-able, an approval is required for a "
            "service-interrupting rollback, or the Idempotency-Key conflicts "
            "with / is still in progress under another request",
        ),
        422: _error_response(422, "Validation failed or Idempotency-Key missing"),
    },
)
async def rollback_changeset(
    request: Request,
    response: Response,
    changeset_id: str,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.OPERATE))],
    body: RollbackRequest | None = None,
) -> OperationAccepted:
    """Enqueue a durable ROLLBACK operation idempotently and answer 202.

    Authorization mirrors the target-owner rule: a principal whose
    ``allowed_target_ids`` excludes the changeset's target is answered with the
    same 404 ``resource_not_found`` as a missing changeset, so a rollback is
    never enqueued for (or its existence leaked to) an unauthorized target.
    The check happens against the resolved changeset BEFORE the idempotent
    execution path; a replayed same-key request re-validates it (the changeset
    row is durable) and returns the pinned ``OperationAccepted``.
    """
    runtime = request.app.state.runtime
    changeset = runtime.change_store.get(changeset_id)
    if changeset is None:
        raise HTTPException(status_code=404, detail="ChangeSet not found")
    if not principal.authorized_for(changeset.target_id):
        raise HTTPException(status_code=404, detail="ChangeSet not found")

    request_sha256 = idempotency_request_sha256(
        method="POST",
        route_key=_ROLLBACK_ROUTE_KEY,
        path_params={"changeset_id": changeset_id},
        canonical_body=json.dumps(
            body.model_dump(mode="json") if body is not None else {},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )

    async def action() -> tuple[int, OperationAccepted]:
        current = runtime.change_store.get(changeset_id)
        if current is None:
            raise ChangeSetNotFoundError(f"changeset {changeset_id} not found")
        if current.status not in _ROLLBACKABLE_STATUSES:
            raise ChangeSetNotRollbackableError(
                f"cannot roll back changeset in status {current.status.value}"
            )
        approval_id = body.approval_id if body is not None else None
        if runtime.changes.interrupts_service(current) and approval_id is None:
            raise RollbackRequiresApprovalError(
                "an approval is required to roll back a service-interrupting changeset"
            )
        operation = runtime.operation_service.enqueue(
            kind=OperationKind.ROLLBACK,
            target_id=current.target_id,
            created_by=principal.principal_id,
            request_payload=json.dumps(
                {"changeset_id": changeset_id, "approval_id": approval_id},
                sort_keys=True,
                separators=(",", ":"),
            ),
            now=datetime.now(UTC),
        )
        return 202, OperationAccepted(operation_id=operation.operation_id)

    try:
        _status_code, payload, replayed = await execute_idempotent(
            service=runtime.idempotency,
            principal=principal,
            method="POST",
            route_key=_ROLLBACK_ROUTE_KEY,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            request_sha256=request_sha256,
            response_type=OperationAccepted,
            action=action,
        )
    except (
        ChangeSetNotFoundError,
        ChangeSetNotRollbackableError,
        RollbackRequiresApprovalError,
    ) as exc:
        raise _to_http(exc) from exc
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return payload


__all__ = ["router"]
