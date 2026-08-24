"""Target product facade routes (``/api/v1/targets``).

Every route is protected by :func:`get_principal` and derives the actor from the
authenticated :class:`Principal`.  Mutations require an ``Idempotency-Key`` via
the :func:`execute_idempotent` machinery, reject actor body fields through
``extra="forbid"`` models, and return only the abbreviated authentication hint —
the full ``authentication_ref`` is never serialized.

Every endpoint declares an explicit ``response_model`` and documents its
``ApiErrorResponse`` failure cases so the stable v1 envelope is part of the
OpenAPI contract.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from incidentlens_control_plane.api.idempotency import (
    execute_idempotent,
    idempotency_request_sha256,
)
from incidentlens_control_plane.api.models import ApiErrorResponse
from incidentlens_control_plane.auth.dependencies import (
    authorize_target,
    get_principal,
    require_scopes,
)
from incidentlens_control_plane.auth.types import Principal, PrincipalScope
from incidentlens_control_plane.operations.types import OperationAccepted, OperationKind
from incidentlens_control_plane.project_registry.store import (
    ProjectAlreadyExists,
    ProjectNotFound,
)
from incidentlens_control_plane.targets.service import (
    TargetDeleteBlocked,
    TargetService,
)
from incidentlens_control_plane.targets.store import (
    TargetAlreadyExists,
    TargetNotFound,
    TargetVersionConflict,
)
from incidentlens_control_plane.targets.types import (
    TargetCreate,
    TargetPatch,
    TargetServiceView,
    TargetView,
)

router = APIRouter(
    prefix="/api/v1/targets",
    tags=["targets"],
    dependencies=[Depends(get_principal)],
)

#: Stable route keys for idempotency request hashing (independent of host/path).
_CREATE_ROUTE_KEY = "/api/v1/targets"
_ITEM_ROUTE_KEY = "/api/v1/targets/{target_id}"
_TEST_ROUTE_KEY = "/api/v1/targets/{target_id}/test"


class _DeletedResult(BaseModel):
    """Internal result persisted for an idempotent DELETE (never sent on the wire)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str


def _error_response(status_code: int, description: str) -> dict[str, object]:
    """Build an OpenAPI ``responses`` entry that carries the v1 error envelope."""
    return {"model": ApiErrorResponse, "description": description}


def _service(request: Request) -> TargetService:
    runtime = request.app.state.runtime
    return runtime.target_service


def _to_http(exc: Exception) -> HTTPException:
    """Map facade/store failures to the stable v1 HTTP envelope."""
    if isinstance(
        exc,
        (TargetAlreadyExists, ProjectAlreadyExists, TargetVersionConflict, TargetDeleteBlocked),
    ):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (TargetNotFound, ProjectNotFound)):
        return HTTPException(status_code=404, detail="Target not found")
    return HTTPException(status_code=500, detail="Internal server error")


@router.post(
    "",
    status_code=201,
    response_model=TargetView,
    operation_id="createTarget",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
        409: _error_response(409, "Target or backing project already exists"),
        422: _error_response(422, "Validation failed or Idempotency-Key missing"),
    },
)
async def create_target(
    request: Request,
    response: Response,
    body: TargetCreate,
    principal: Annotated[Principal, Depends(get_principal)],
) -> TargetView:
    """Create a product target backed by a fresh internal project."""
    canonical_body = json.dumps(
        body.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    request_sha256 = idempotency_request_sha256(
        method="POST",
        route_key=_CREATE_ROUTE_KEY,
        path_params={},
        canonical_body=canonical_body,
    )

    async def action() -> tuple[int, TargetView]:
        view = _service(request).create_target(body, now=datetime.now(UTC))
        return 201, view

    try:
        _status_code, payload, replayed = await execute_idempotent(
            service=request.app.state.runtime.idempotency,
            principal=principal,
            method="POST",
            route_key=_CREATE_ROUTE_KEY,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            request_sha256=request_sha256,
            response_type=TargetView,
            action=action,
        )
    except (TargetAlreadyExists, ProjectAlreadyExists) as exc:
        raise _to_http(exc) from exc
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return payload


@router.get(
    "",
    response_model=list[TargetView],
    operation_id="listTargets",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
    },
)
async def list_targets(
    request: Request,
) -> list[TargetView]:
    """Return every product target, binding pre-existing registry targets."""
    return _service(request).list_targets()


@router.get(
    "/{target_id}",
    response_model=TargetView,
    operation_id="getTarget",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
        404: _error_response(404, "Target not found"),
    },
)
async def get_target(
    request: Request,
    target_id: str,
    principal: Annotated[Principal, Depends(get_principal)],
) -> TargetView:
    """Return one product target, lazily binding pre-existing targets."""
    authorize_target(principal, target_id)
    try:
        return _service(request).get_target(target_id)
    except TargetNotFound as exc:
        raise _to_http(exc) from exc


@router.get(
    "/{target_id}/services",
    response_model=list[TargetServiceView],
    operation_id="listTargetServices",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
        404: _error_response(404, "Target not found"),
    },
)
async def list_target_services(
    request: Request,
    target_id: str,
    principal: Annotated[Principal, Depends(get_principal)],
) -> list[TargetServiceView]:
    """Return the target's services, resolved through ProjectRegistry."""
    authorize_target(principal, target_id)
    try:
        return _service(request).services_for_target(target_id)
    except TargetNotFound as exc:
        raise _to_http(exc) from exc


@router.patch(
    "/{target_id}",
    response_model=TargetView,
    operation_id="patchTarget",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
        404: _error_response(404, "Target not found"),
        409: _error_response(409, "Stale expected_version or idempotency conflict"),
        422: _error_response(422, "Validation failed or Idempotency-Key missing"),
    },
)
async def patch_target(
    request: Request,
    response: Response,
    target_id: str,
    body: TargetPatch,
    principal: Annotated[Principal, Depends(get_principal)],
) -> TargetView:
    """Patch a product target under optimistic concurrency."""
    authorize_target(principal, target_id)
    canonical_body = json.dumps(
        body.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    request_sha256 = idempotency_request_sha256(
        method="PATCH",
        route_key=_ITEM_ROUTE_KEY,
        path_params={"target_id": target_id},
        canonical_body=canonical_body,
    )

    async def action() -> tuple[int, TargetView]:
        view = _service(request).patch_target(target_id, body, now=datetime.now(UTC))
        return 200, view

    try:
        _status_code, payload, replayed = await execute_idempotent(
            service=request.app.state.runtime.idempotency,
            principal=principal,
            method="PATCH",
            route_key=_ITEM_ROUTE_KEY,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            request_sha256=request_sha256,
            response_type=TargetView,
            action=action,
        )
    except (TargetNotFound, TargetVersionConflict) as exc:
        raise _to_http(exc) from exc
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return payload


@router.delete(
    "/{target_id}",
    status_code=204,
    operation_id="deleteTarget",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
        404: _error_response(404, "Target not found"),
        409: _error_response(
            409, "Target is referenced by an active investigation"
        ),
        422: _error_response(422, "Idempotency-Key missing"),
    },
)
async def delete_target(
    request: Request,
    response: Response,
    target_id: str,
    principal: Annotated[Principal, Depends(get_principal)],
) -> None:
    """Delete a product target, blocked while investigations reference it."""
    authorize_target(principal, target_id)
    request_sha256 = idempotency_request_sha256(
        method="DELETE",
        route_key=_ITEM_ROUTE_KEY,
        path_params={"target_id": target_id},
        canonical_body="{}",
    )

    async def action() -> tuple[int, _DeletedResult]:
        _service(request).delete_target(target_id, now=datetime.now(UTC))
        return 204, _DeletedResult(target_id=target_id)

    try:
        _status_code, _payload, replayed = await execute_idempotent(
            service=request.app.state.runtime.idempotency,
            principal=principal,
            method="DELETE",
            route_key=_ITEM_ROUTE_KEY,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            request_sha256=request_sha256,
            response_type=_DeletedResult,
            action=action,
        )
    except (TargetNotFound, TargetDeleteBlocked, TargetVersionConflict) as exc:
        raise _to_http(exc) from exc
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"


@router.post(
    "/{target_id}/test",
    status_code=202,
    response_model=OperationAccepted,
    operation_id="testTarget",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
        404: _error_response(404, "Target not found"),
        409: _error_response(
            409,
            "The Idempotency-Key conflicts with / is still in progress "
            "under another request",
        ),
        422: _error_response(422, "Validation failed or Idempotency-Key missing"),
    },
)
async def test_target(
    request: Request,
    response: Response,
    target_id: str,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.OPERATE))],
) -> OperationAccepted:
    """Enqueue a durable TARGET_TEST reachability probe idempotently.

    The probe executes through the registered target-test handler on the
    operation dispatcher (connect + capability check) and answers ``202``
    immediately with the new ``operation_id`` so the caller can follow it on the
    ``/api/v1/operations`` read surface.
    """
    authorize_target(principal, target_id)
    request_sha256 = idempotency_request_sha256(
        method="POST",
        route_key=_TEST_ROUTE_KEY,
        path_params={"target_id": target_id},
        canonical_body="{}",
    )

    async def action() -> tuple[int, OperationAccepted]:
        runtime = request.app.state.runtime
        # Resolve the facade target (lazily binding pre-existing registry
        # targets) so the enqueued probe always carries a valid product target.
        _service(request).get_target(target_id)
        operation = runtime.operation_service.enqueue(
            kind=OperationKind.TARGET_TEST,
            target_id=target_id,
            created_by=principal.principal_id,
            request_payload=json.dumps({}, sort_keys=True, separators=(",", ":")),
            now=datetime.now(UTC),
        )
        return 202, OperationAccepted(operation_id=operation.operation_id)

    try:
        _status_code, payload, replayed = await execute_idempotent(
            service=request.app.state.runtime.idempotency,
            principal=principal,
            method="POST",
            route_key=_TEST_ROUTE_KEY,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            request_sha256=request_sha256,
            response_type=OperationAccepted,
            action=action,
        )
    except TargetNotFound as exc:
        raise _to_http(exc) from exc
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return payload
