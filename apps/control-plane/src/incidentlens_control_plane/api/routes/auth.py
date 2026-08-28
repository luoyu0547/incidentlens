"""Authentication routes for the versioned product API.

Two routers leave ``routes/auth.py``:

- :const:`session_router` -- the public session intake
  (``POST /api/v1/auth/session``) that exchanges a bearer token for a signed
  browser cookie.  It is deliberately exempt from the auth dependency chain:
  it is how a caller *becomes* authenticated.
- :const:`auth_router` -- the protected auth members (``GET /api/v1/principal``,
  ``POST /api/v1/auth/logout``) that apply :func:`get_principal` at router
  level.  The same dependency function is reusable by future ``/ws/v1`` and
  ``/events/v1`` routers.

Every endpoint declares an explicit ``response_model`` and documents its
``ApiErrorResponse`` failure cases so the stable v1 envelope is part of the
OpenAPI contract -- including the 422 ``request_validation_failed`` case that
enforces the v1 rule that actor identity is never accepted from the request
body (any unexpected field such as ``{"created_by": ...}`` is rejected before
the handler runs).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from incidentlens_control_plane.api.models import ApiErrorResponse
from incidentlens_control_plane.auth.dependencies import get_principal
from incidentlens_control_plane.auth.service import AuthService
from incidentlens_control_plane.auth.types import Principal, SessionCreated

session_router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

#: Protected members; session creation is exempt and lives on session_router.
auth_router = APIRouter(
    prefix="/api/v1",
    tags=["auth"],
    dependencies=[Depends(get_principal)],
)


def _error_response(status_code: int, description: str) -> dict[str, object]:
    """Build an OpenAPI ``responses`` entry that carries the v1 error envelope."""
    return {"model": ApiErrorResponse, "description": description}


class EmptyBody(BaseModel):
    """A body parcel that accepts no fields.

    Enforces the v1 rule that actor identity is never accepted from the request
    body: any present field (for example ``created_by``) fails validation with
    a 422 ``request_validation_failed`` envelope.
    """

    model_config = ConfigDict(extra="forbid")


def _get_auth_service(request: Request) -> AuthService:
    runtime = request.app.state.runtime
    return runtime.auth


@session_router.post(
    "/session",
    status_code=200,
    response_model=SessionCreated,
    operation_id="createSession",
    responses={
        401: _error_response(401, "Missing or invalid bearer token"),
        422: _error_response(422, "Unexpected request body fields"),
    },
)
async def create_session(
    request: Request,
    response: Response,
    body: EmptyBody | None = None,
) -> SessionCreated:
    """Exchange a bearer token for a signed ``incidentlens_session`` cookie.

    The cookie is HttpOnly, SameSite=Strict, Path=/ and (in production) Secure.
    The response body carries the CSRF nonce the browser must echo on later
    cookie-authenticated mutations.
    """
    del body  # the route intentionally accepts no request fields
    service = _get_auth_service(request)
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer token required")
    token = authorization.removeprefix("Bearer ").strip()
    issued = service.issue_session(token)
    if issued is None:
        raise HTTPException(status_code=401, detail="invalid bearer token")

    response.set_cookie(
        service.cookie_name,
        issued.cookie_value,
        httponly=True,
        samesite="strict",
        secure=service.secure_cookies,
        path="/",
        max_age=service.session_ttl_seconds,
    )
    return issued.session


@auth_router.get(
    "/principal",
    response_model=Principal,
    operation_id="getCurrentPrincipal",
    responses={
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "Permission denied"),
        422: _error_response(422, "Request validation failed"),
    },
)
async def get_current_principal(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """Return the principal that authenticated the current request."""
    return principal


@auth_router.post(
    "/auth/logout",
    status_code=204,
    operation_id="logout",
    responses={
        204: {"description": "Session ended; session cookie cleared"},
        401: _error_response(401, "Authentication required"),
        403: _error_response(403, "CSRF token missing or invalid"),
        422: _error_response(422, "Unexpected request body fields"),
    },
)
async def logout(
    request: Request,
    body: EmptyBody | None = None,
) -> Response:
    """End the browser session by clearing the session cookie."""
    del body  # the route intentionally accepts no request fields
    service = _get_auth_service(request)
    response = Response(status_code=204)
    response.delete_cookie(service.cookie_name, path="/")
    return response
