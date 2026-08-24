"""AuthService: digest-verified bearer tokens and signed browser sessions.

The service owns two stateless credentials:

- **Bearer tokens** are compared by SHA-256 digest with
  :func:`hmac.compare_digest`, so the runtime never holds a plaintext token and
  a timing side-channel does not leak the digest.
- **Browser sessions** are a signed cookie: URL-safe base64 JSON payload plus
  an HMAC-SHA256 signature over that payload.  The payload holds the principal
  id, issued/expiry epoch seconds and a CSRF nonce, so cookie-authenticated
  mutations can be pinned to the session with no server-side session store.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass

from incidentlens_control_plane.auth.types import (
    AuthenticationMethod,
    AuthProfile,
    Principal,
    SessionCreated,
    SessionData,
)

#: Namespace marker so signature inputs are unambiguous across payload shapes.
_DEFAULT_SESSION_TTL_SECONDS = 3_600


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A freshly issued signed session: the wire body plus its cookie value."""

    session: SessionCreated
    cookie_value: str


def profiles_from_json(raw: str | None) -> tuple[AuthProfile, ...]:
    """Parse the ``INCIDENTLENS_AUTH_PROFILES_JSON`` payload into profiles.

    ``None`` or empty input yields no profiles (everything unauthenticated),
    and invalid JSON or an unknown profile field fails fast so a misconfigured
    deployment never boots with an open auth surface.
    """
    if not raw or not raw.strip():
        return ()
    import json as _json

    data = _json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("auth profiles must be a JSON array")
    return tuple(AuthProfile.model_validate(item) for item in data)


class AuthService:
    """Static deployment-profile authentication plus signed session cookies."""

    def __init__(
        self,
        profiles: Iterable[AuthProfile],
        signing_key: str,
        *,
        session_ttl_seconds: int = _DEFAULT_SESSION_TTL_SECONDS,
        secure_cookies: bool = True,
        cookie_name: str = "incidentlens_session",
    ) -> None:
        self.session_ttl_seconds = session_ttl_seconds
        self.secure_cookies = secure_cookies
        self.cookie_name = cookie_name

        self._profiles_by_id: dict[str, AuthProfile] = {
            profile.principal_id: profile for profile in profiles
        }
        self._digest_to_profile: dict[str, AuthProfile] = {
            profile.token_digest.lower(): profile for profile in profiles
        }
        self._signing_key = signing_key.encode("utf-8")

    # -- bearer credentials ---------------------------------------------------

    def authenticate_bearer(self, token: str) -> Principal | None:
        """Resolve a bearer token to a principal, or ``None`` when invalid."""
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest().lower()
        for stored_digest, profile in self._digest_to_profile.items():
            if hmac.compare_digest(digest, stored_digest):
                return self._principal_for(profile, AuthenticationMethod.BEARER)
        return None

    def principal_for_id(
        self, principal_id: str, method: AuthenticationMethod
    ) -> Principal | None:
        """Resolve a profile by id under an explicit authentication method."""
        profile = self._profiles_by_id.get(principal_id)
        if profile is None:
            return None
        return self._principal_for(profile, method)

    def _principal_for(
        self, profile: AuthProfile, method: AuthenticationMethod
    ) -> Principal:
        return Principal(
            principal_id=profile.principal_id,
            display_name=profile.display_name,
            scopes=profile.scopes,
            allowed_target_ids=profile.allowed_target_ids,
            authentication_method=method,
        )

    # -- signed session cookies ----------------------------------------------

    def issue_session(self, token: str) -> IssuedSession | None:
        """Exchange a valid bearer token for a signed session cookie.

        Returns the response body model together with the signed cookie value,
        or ``None`` when the token is not a known profile.
        """
        principal = self.authenticate_bearer(token)
        if principal is None:
            return None
        # The established session's principal authenticates via the cookie it
        # just received, not the bearer token that created it.
        session_principal = self.principal_for_id(
            principal.principal_id, AuthenticationMethod.SESSION_COOKIE
        )
        if session_principal is None:
            return None
        now = int(time.time())
        data = SessionData(
            principal_id=session_principal.principal_id,
            issued_at=now,
            expires_at=now + self.session_ttl_seconds,
            nonce=secrets.token_hex(16),
        )
        created = SessionCreated(
            principal=session_principal,
            csrf_token=data.nonce,
            expires_at=data.expires_at,
        )
        return IssuedSession(
            session=created,
            cookie_value=self.sign_session_data(data),
        )

    def sign_session_data(self, data: SessionData) -> str:
        """Produce the signed ``incidentlens_session`` cookie value."""
        payload = {
            "principal_id": data.principal_id,
            "issued_at": data.issued_at,
            "expires_at": data.expires_at,
            "nonce": data.nonce,
        }
        payload_bytes = json.dumps(
            payload, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("ascii")
        signature = hmac.new(
            self._signing_key, payload_b64.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return f"{payload_b64}.{signature}"

    def verify_session_cookie(self, cookie_value: str) -> SessionData | None:
        """Verify a signed cookie and return its payload when valid/not expired."""
        if not cookie_value:
            return None
        try:
            payload_b64, signature = cookie_value.rsplit(".", 1)
        except ValueError:
            return None
        expected = hmac.new(
            self._signing_key, payload_b64.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return None
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8")
            )
            data = SessionData(
                principal_id=str(payload["principal_id"]),
                issued_at=int(payload["issued_at"]),
                expires_at=int(payload["expires_at"]),
                nonce=str(payload["nonce"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if data.expires_at < int(time.time()):
            return None
        return data

    def csrf_valid_nonce(
        self, header_value: str | None, expected_nonce: str
    ) -> bool:
        """True when a mutation's ``X-CSRF-Token`` matches the session nonce."""
        if not header_value:
            return False
        return hmac.compare_digest(header_value, expected_nonce)
