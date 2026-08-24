"""Durable operation routes (``/api/v1/operations``).

Every route is protected by :func:`get_principal` and derives the actor from the
authenticated :class:`Principal`.  Reads require the ``READ`` scope; the
cancellation mutation requires ``OPERATE``, carries an ``Idempotency-Key`` via
the :func:`execute_idempotent` machinery, and is idempotent by semantics so
repeated cancels succeed and return the same ``OperationView``.

Authorization is the operation-owner rule: a principal may address an operation
when its ``allowed_target_ids`` includes the operation's ``target_id`` (or the
principal is unrestricted) OR the principal created the operation.  Operations
on unauthorized targets surface as 404 ``resource_not_found`` so their existence
is never leaked.

Every endpoint declares an explicit ``response_model`` and documents its
``ApiErrorResponse`` failure cases so the stable v1 envelope is part of the
OpenAPI contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, ClassVar

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from incidentlens_control_plane.api.errors import ApiProblem
from incidentlens_control_plane.api.idempotency import (
    execute_idempotent,
    idempotency_request_sha256,
)
from incidentlens_control_plane.api.models import ApiErrorResponse
from incidentlens_control_plane.auth.dependencies import get_principal, require_scopes
from incidentlens_control_plane.auth.types import Principal, PrincipalScope
from incidentlens_control_plane.operations.service import OperationService
from incidentlens_control_plane.operations.state_machine import OperationNotCancellable
from incidentlens_control_plane.operations.store import OperationNotFound
from incidentlens_control_plane.operations.types import Operation, OperationView

router = APIRouter(
    prefix="/api/v1/operations",
    tags=["operations"],
    dependencies=[Depends(get_principal)],
)

_CANCEL_ROUTE_KEY = "/api/v1/operations/{operation_id}/cancel"


class OperationNotCancellableProblem(ApiProblem):
    """Stable 409 envelope for cancelling a terminal non-cancelled operation."""

    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "operation_not_cancellable"
    message: ClassVar[str] = (
        "operation is in a terminal state and cannot be cancelled"
    )


def _error_response(status_code: int, description: str) -> dict[str, object]:
    """Build an OpenAPI ``responses`` entry that carries the v1 error envelope."""
    return {"model": ApiErrorResponse, "description": description}


def _service(request: Request) -> OperationService:
    runtime = request.app.state.runtime
    return runtime.operation_service


def _authorized(principal: Principal, operation: Operation) -> bool:
    """True when *principal* owns *operation* (target rule or creation rule)."""
    return principal.authorized_for(operation.target_id) or (
        principal.principal_id == operation.created_by
    )


def _to_http(exc: Exception) -> Exception:
    """Map service failures to stable v1 HTTP exceptions/problems."""
    if isinstance(exc, OperationNotFound):
        return HTTPException(status_code=404, detail="Operation not found")
    if isinstance(exc, OperationNotCancellable):
        return OperationNotCancellableProblem()
    return HTTPException(status_code=500, detail="Internal server error")


@router.get(
    "/{operation_id}",
    response_model=OperationView,
    operation_id="getOperation",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
        404: _error_response(404, "Operation not found"),
    },
)
async def get_operation(
    request: Request,
    operation_id: str,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.READ))],
) -> OperationView:
    """Return one durable operation to its owner."""
    service = _service(request)
    try:
        operation = service.get_operation(operation_id)
    except OperationNotFound as exc:
        raise _to_http(exc) from exc
    if not _authorized(principal, operation):
        raise HTTPException(status_code=404, detail="Operation not found")
    return service.to_view(operation)


@router.post(
    "/{operation_id}/cancel",
    response_model=OperationView,
    operation_id="cancelOperation",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
        404: _error_response(404, "Operation not found"),
        409: _error_response(
            409, "Operation is terminal and cannot be cancelled"
        ),
        422: _error_response(422, "Validation failed or Idempotency-Key missing"),
    },
)
async def cancel_operation(
    request: Request,
    response: Response,
    operation_id: str,
    principal: Annotated[Principal, Depends(require_scopes(PrincipalScope.OPERATE))],
) -> OperationView:
    """Cancel a durable operation idempotently (owner only)."""
    service = _service(request)
    try:
        operation = service.get_operation(operation_id)
    except OperationNotFound as exc:
        raise _to_http(exc) from exc
    if not _authorized(principal, operation):
        raise HTTPException(status_code=404, detail="Operation not found")

    request_sha256 = idempotency_request_sha256(
        method="POST",
        route_key=_CANCEL_ROUTE_KEY,
        path_params={"operation_id": operation_id},
        canonical_body="{}",
    )

    async def action() -> tuple[int, OperationView]:
        updated = service.cancel(operation_id, now=datetime.now(UTC))
        return 200, service.to_view(updated)

    try:
        _status_code, payload, replayed = await execute_idempotent(
            service=request.app.state.runtime.idempotency,
            principal=principal,
            method="POST",
            route_key=_CANCEL_ROUTE_KEY,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            request_sha256=request_sha256,
            response_type=OperationView,
            action=action,
        )
    except (OperationNotFound, OperationNotCancellable) as exc:
        raise _to_http(exc) from exc
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return payload


__all__ = ["router"]
