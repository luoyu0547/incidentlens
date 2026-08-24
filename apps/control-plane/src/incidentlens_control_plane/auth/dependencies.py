"""Reusable auth dependencies for the v1 product API.

:func:`get_principal` is the one dependency every protected route shares: it
resolves the caller from either a ``Bearer`` token or the signed
``incidentlens_session`` cookie, and for cookie sessions it enforces the CSRF
rule -- any state-changing method (POST/PUT/PATCH/DELETE) must carry an
``X-CSRF-Token`` matching the session's signed nonce.  Bearer principals are
programmatic and are exempt from CSRF by construction.

The dependency is intentionally a plain FastAPI dependency function so the
future ``/ws/v1`` and ``/events/v1`` routers can apply it router-level the same
way this task applies it to the auth router's protected members.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from incidentlens_control_plane.auth.service import AuthService
from incidentlens_control_plane.auth.types import (
    AuthenticationMethod,
    Principal,
    PrincipalScope,
)

#: HTTP methods that change server state and therefore require CSRF protection
#: when authenticated through a browser session cookie.
_CSRF_SENSITIVE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Request header carrying the anti-CSRF token for cookie-authenticated edits.
CSRF_HEADER = "X-CSRF-Token"

#: The signed session cookie name.
SESSION_COOKIE_NAME = "incidentlens_session"


def _auth_service(request: Request) -> AuthService:
    runtime = request.app.state.runtime
    if runtime is None:
        raise HTTPException(status_code=401, detail="authentication unavailable")
    return runtime.auth


async def get_principal(request: Request) -> Principal:
    """Resolve and return the authenticated principal for the request.

    Precedence is bearer-first: a valid ``Authorization: Bearer`` token wins
    over a session cookie and does not require CSRF.  Otherwise a valid, unexpired
    ``incidentlens_session`` cookie resolves the principal, subject to the CSRF
    rule for mutating methods.  Unauthenticated requests raise ``HTTPException``
    401, which the v1 error envelope normalizes to ``authentication_required``.
    """
    service = _auth_service(request)

    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if token:
            principal = service.authenticate_bearer(token)
            if principal is not None:
                return principal

    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        session = service.verify_session_cookie(cookie)
        if session is not None:
            principal = service.principal_for_id(
                session.principal_id, AuthenticationMethod.SESSION_COOKIE
            )
            if principal is not None:
                _enforce_csrf_for_mutation(request, service, session.nonce)
                return principal

    raise HTTPException(status_code=401, detail="authentication required")


def _enforce_csrf_for_mutation(
    request: Request, service: AuthService, expected_nonce: str
) -> None:
    """Reject a cookie-authenticated mutation that lacks a matching CSRF token."""
    if request.method not in _CSRF_SENSITIVE_METHODS:
        return
    if not service.csrf_valid_nonce(request.headers.get(CSRF_HEADER), expected_nonce):
        raise HTTPException(
            status_code=403, detail="CSRF token missing or invalid"
        )


def require_scopes(
    *scopes: PrincipalScope,
) -> Callable[..., Principal]:
    """Build a dependency requiring the caller to hold every listed scope.

    Missing scopes surface as 403 ``permission_denied`` under the v1 envelope.
    """

    async def _require(
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> Principal:
        missing = [scope for scope in scopes if scope not in principal.scopes]
        if missing:
            names = ", ".join(scope.value for scope in missing)
            raise HTTPException(
                status_code=403,
                detail=f"missing required scopes: {names}",
            )
        return principal

    return _require


def authorize_target(principal: Principal, target_id: str) -> None:
    """Raise 403 when *principal* is not allowed to address *target_id*.

    A principal with no explicit ``allowed_target_ids`` may address any target.
    """
    if principal.allowed_target_ids is not None and not principal.authorized_for(
        target_id
    ):
        raise HTTPException(
            status_code=403,
            detail=f"target '{target_id}' is not allowed for this principal",
        )
