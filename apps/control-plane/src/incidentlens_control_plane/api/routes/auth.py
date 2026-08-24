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

V1 request bodies never accept actor identity: every body model here is an
empty ``extra="forbid"`` parcel so any unexpected field (such as
``{"created_by": ...}``) is rejected with a stable 422 before the handler runs.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from incidentlens_control_plane.auth.dependencies import get_principal
from incidentlens_control_plane.auth.service import AuthService
from incidentlens_control_plane.auth.types import Principal

session_router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

#: Protected members; session creation is exempt and lives on session_router.
auth_router = APIRouter(
    prefix="/api/v1",
    tags=["auth"],
    dependencies=[Depends(get_principal)],
)


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


@session_router.post("/session", status_code=200)
async def create_session(
    request: Request,
    body: EmptyBody | None = None,
) -> Response:
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

    response = JSONResponse(content=jsonable_encoder(issued.session))
    response.set_cookie(
        service.cookie_name,
        issued.cookie_value,
        httponly=True,
        samesite="strict",
        secure=service.secure_cookies,
        path="/",
        max_age=service.session_ttl_seconds,
    )
    return response


@auth_router.get("/principal", response_model=Principal)
async def get_current_principal(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """Return the principal that authenticated the current request."""
    return principal


@auth_router.post("/auth/logout", status_code=204)
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
