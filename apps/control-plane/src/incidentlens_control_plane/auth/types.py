"""Principal and authentication wire types for the product API.

The vocabulary here is the stable identity model for the v1 surface: every
requestable operation is performed *as* a :class:`Principal` resolved by the
auth dependency stack, and principals are described by immutable ``frozenset``
scopes so authorization checks can never observe a partially-mutated grant.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PrincipalScope(StrEnum):
    """Coarse-grained capability buckets granted to a principal."""

    READ = "read"
    OPERATE = "operate"
    APPROVE = "approve"
    ADMIN = "admin"


class AuthenticationMethod(StrEnum):
    """How a principal proved its identity for the current request."""

    BEARER = "bearer"
    SESSION_COOKIE = "session_cookie"


class Principal(BaseModel):
    """An authenticated actor for the duration of one request.

    ``allowed_target_ids`` is ``None`` when the principal is allowed to address
    every target; otherwise the explicit allow-list is the only set of targets
    the principal may reference.  The model is frozen and rejects unknown
    fields so identity can never be smuggled through a request body.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str
    display_name: str
    scopes: frozenset[PrincipalScope]
    allowed_target_ids: frozenset[str] | None = None
    authentication_method: AuthenticationMethod

    def can(self, scope: PrincipalScope) -> bool:
        """True when the principal holds *scope*."""
        return scope in self.scopes

    def authorized_for(self, target_id: str) -> bool:
        """True when the principal may address *target_id*."""
        if self.allowed_target_ids is None:
            return True
        return target_id in self.allowed_target_ids


class AuthProfile(BaseModel):
    """A static deployment profile backed by a SHA-256 token *digest*.

    The on-wire profile JSON contains the hex SHA-256 digest of the bearer
    token, never the token itself.  Profiles are immutable and reject unknown
    fields so a malformed deployment payload fails closed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str
    display_name: str
    scopes: frozenset[PrincipalScope]
    allowed_target_ids: frozenset[str] | None = None
    token_digest: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class SessionData(BaseModel):
    """The signed payload carried inside a browser session cookie.

    The cookie's value is a URL-safe base64 JSON payload concatenated with an
    HMAC-SHA256 signature; the CSRF nonce lives in the same signed payload so
    cookie-authenticated mutations can be pinned to the session without any
    server-side store.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str
    issued_at: int
    expires_at: int
    nonce: str


class SessionCreated(BaseModel):
    """Response model for ``POST /api/v1/auth/session``.

    The CSRF token is returned in the body because browser-based clients need
    it to authenticate cookie sessions on later mutations; it is also recoverable
    from the signed cookie by any client that knows the signing key, but the
    response is the supported channel.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal: Principal
    csrf_token: str
    expires_at: int
