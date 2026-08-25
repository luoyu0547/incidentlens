"""WebSocket credential resolution.

Reuses the same bearer-token and signed-session verification as the HTTP
``get_principal`` dependency, but for read-only stream connections: a WebSocket
never mutates state, so CSRF does not apply (the browser cannot forge a
cross-site WebSocket frame anyway), and the CSRF nonce is not required.

Resolves a :class:`Principal` or ``None`` when the credentials are missing,
unknown, or expired.
"""

from __future__ import annotations

from fastapi import WebSocket

from incidentlens_control_plane.auth.dependencies import SESSION_COOKIE_NAME
from incidentlens_control_plane.auth.service import AuthService
from incidentlens_control_plane.auth.types import (
    AuthenticationMethod,
    Principal,
)


def resolve_ws_principal(websocket: WebSocket) -> Principal | None:
    """Resolve and return the authenticated principal for a WebSocket, if any."""
    runtime = websocket.app.state.runtime
    if runtime is None:
        return None
    service: AuthService = runtime.auth

    authorization = websocket.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if token:
            principal = service.authenticate_bearer(token)
            if principal is not None:
                return principal

    cookie = websocket.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        session = service.verify_session_cookie(cookie)
        if session is not None:
            principal = service.principal_for_id(
                session.principal_id, AuthenticationMethod.SESSION_COOKIE
            )
            if principal is not None:
                return principal

    return None
