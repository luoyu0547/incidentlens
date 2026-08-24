"""Unit tests for AuthService: digest-verified bearer tokens and signed sessions.

These tests exercise the service directly (no HTTP).  The HTTP behaviours --
auth enforcement, cookie round-trip, CSRF rejection -- live in
``tests/api_v1/test_auth.py``.
"""

from __future__ import annotations

import time

import pytest
from incidentlens_control_plane.auth.dependencies import (
    authorize_target,
    require_scopes,
)
from incidentlens_control_plane.auth.service import AuthService, profiles_from_json
from incidentlens_control_plane.auth.types import (
    AuthenticationMethod,
    AuthProfile,
    Principal,
    PrincipalScope,
    SessionData,
)

from auth.helpers import (
    AUTH_PROFILES_JSON,
    OPERATOR_A_PROFILE_ID,
    OPERATOR_A_TOKEN,
    OPERATOR_A_TOKEN_DIGEST,
)


def _service(**kwargs) -> AuthService:
    profiles = profiles_from_json(AUTH_PROFILES_JSON)
    return AuthService(
        profiles,
        signing_key="test-signing-key",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Profile parsing
# ---------------------------------------------------------------------------


def test_profiles_from_json_parses_shared_profile() -> None:
    profiles = profiles_from_json(AUTH_PROFILES_JSON)
    assert len(profiles) == 1
    assert profiles[0].principal_id == OPERATOR_A_PROFILE_ID
    assert PrincipalScope.READ in profiles[0].scopes


def test_profiles_from_json_accepts_empty_and_none() -> None:
    assert profiles_from_json(None) == ()
    assert profiles_from_json("") == ()
    assert profiles_from_json("   ") == ()


def test_profiles_from_json_rejects_non_list() -> None:
    with pytest.raises(ValueError):
        profiles_from_json('{"principal_id": "x"}')


def test_profile_rejects_plaintext_token_digest_shape() -> None:
    with pytest.raises(Exception):
        AuthProfile.model_validate(
            {
                "principal_id": "x",
                "display_name": "X",
                "scopes": ["read"],
                "token_digest": "not-a-64-hex-digest",
            }
        )


# ---------------------------------------------------------------------------
# Bearer credentials
# ---------------------------------------------------------------------------


def test_bearer_token_resolves_to_principal() -> None:
    service = _service()
    principal = service.authenticate_bearer(OPERATOR_A_TOKEN)
    assert principal is not None
    assert principal.principal_id == OPERATOR_A_PROFILE_ID
    assert principal.authentication_method == AuthenticationMethod.BEARER
    assert principal.can(PrincipalScope.ADMIN)
    assert principal.authorized_for("any-target")


def test_bearer_token_plaintext_is_never_stored() -> None:
    service = _service()
    profiles = list(service._profiles_by_id.values())
    assert all(OPERATOR_A_TOKEN not in profile.model_dump().values() for profile in profiles)


def test_invalid_bearer_token_returns_none() -> None:
    service = _service()
    assert service.authenticate_bearer("wrong-token") is None


def test_duplicate_bearer_digest_still_resolves() -> None:
    shared = OPERATOR_A_TOKEN_DIGEST
    profiles = (
        AuthProfile(
            principal_id="first",
            display_name="First",
            scopes=frozenset({PrincipalScope.READ}),
            token_digest=shared,
        ),
        AuthProfile(
            principal_id="second",
            display_name="Second",
            scopes=frozenset({PrincipalScope.READ}),
            token_digest=shared,
        ),
    )
    service = AuthService(profiles, signing_key="key")
    principal = service.authenticate_bearer(OPERATOR_A_TOKEN)
    # A digest collision is a deployment misconfiguration; the service must
    # still resolve deterministically (last-inserted profile wins) -- and
    # never reject the token outright.
    assert principal is not None
    assert principal.principal_id in {"first", "second"}


def test_principal_for_id_uses_supplied_method() -> None:
    service = _service()
    principal = service.principal_for_id(
        OPERATOR_A_PROFILE_ID, AuthenticationMethod.SESSION_COOKIE
    )
    assert principal is not None
    assert principal.authentication_method == AuthenticationMethod.SESSION_COOKIE


def test_principal_for_unknown_id_returns_none() -> None:
    assert _service().principal_for_id(
        "nope", AuthenticationMethod.SESSION_COOKIE
    ) is None


# ---------------------------------------------------------------------------
# Signed sessions
# ---------------------------------------------------------------------------


def test_issue_session_exchanges_token_for_signed_session() -> None:
    service = _service(session_ttl_seconds=120)
    issued = service.issue_session(OPERATOR_A_TOKEN)
    assert issued is not None
    assert issued.session.principal.authentication_method == AuthenticationMethod.SESSION_COOKIE
    assert issued.session.expires_at > int(time.time())
    assert len(issued.session.csrf_token) == 32
    assert "." in issued.cookie_value


def test_issue_session_rejects_unknown_token() -> None:
    assert _service().issue_session("wrong") is None


def test_session_cookie_round_trips() -> None:
    service = _service()
    issued = service.issue_session(OPERATOR_A_TOKEN)
    assert issued is not None
    data = service.verify_session_cookie(issued.cookie_value)
    assert data is not None
    assert data.principal_id == OPERATOR_A_PROFILE_ID
    assert data.nonce == issued.session.csrf_token


def test_tampered_session_cookie_is_rejected() -> None:
    service = _service()
    issued = service.issue_session(OPERATOR_A_TOKEN)
    assert issued is not None
    payload, signature = issued.cookie_value.rsplit(".", 1)
    flipped = "0" if signature[0] != "0" else "1"
    tampered = f"{payload}.{flipped + signature[1:]}"
    assert service.verify_session_cookie(tampered) is None


def test_expired_session_cookie_is_rejected() -> None:
    service = _service()
    data = SessionData(
        principal_id=OPERATOR_A_PROFILE_ID,
        issued_at=int(time.time()) - 200,
        expires_at=int(time.time()) - 100,
        nonce="a" * 32,
    )
    cookie = service.sign_session_data(data)
    assert service.verify_session_cookie(cookie) is None


def test_future_issued_at_session_cookie_is_rejected() -> None:
    service = _service()
    future = int(time.time()) + 7_200
    data = SessionData(
        principal_id=OPERATOR_A_PROFILE_ID,
        issued_at=future,
        expires_at=future + 3_600,
        nonce="c" * 32,
    )
    cookie = service.sign_session_data(data)
    assert service.verify_session_cookie(cookie) is None


def test_garbage_cookie_is_rejected() -> None:
    assert _service().verify_session_cookie("not-a-cookie") is None
    assert _service().verify_session_cookie("") is None


def test_signature_is_deterministic_per_payload() -> None:
    service = _service()
    data = SessionData(
        principal_id="op",
        issued_at=10,
        expires_at=20,
        nonce="b" * 32,
    )
    assert service.sign_session_data(data) == service.sign_session_data(data)


def test_csrf_nonce_validation() -> None:
    service = _service()
    issued = service.issue_session(OPERATOR_A_TOKEN)
    assert issued is not None
    nonce = issued.session.csrf_token
    assert service.csrf_valid_nonce(nonce, nonce) is True
    assert service.csrf_valid_nonce("wrong", nonce) is False
    assert service.csrf_valid_nonce(None, nonce) is False


# ---------------------------------------------------------------------------
# Authorization helpers
# ---------------------------------------------------------------------------


async def test_require_scopes_passes_when_all_held() -> None:
    principal = Principal(
        principal_id="p",
        display_name="P",
        scopes=frozenset({PrincipalScope.READ, PrincipalScope.OPERATE}),
        authentication_method=AuthenticationMethod.BEARER,
    )
    dependency = require_scopes(PrincipalScope.READ, PrincipalScope.OPERATE)
    assert await dependency(principal) == principal


async def test_require_scopes_rejects_missing_scope() -> None:
    from fastapi import HTTPException

    principal = Principal(
        principal_id="p",
        display_name="P",
        scopes=frozenset({PrincipalScope.READ}),
        authentication_method=AuthenticationMethod.BEARER,
    )
    dependency = require_scopes(PrincipalScope.ADMIN)
    with pytest.raises(HTTPException) as excinfo:
        await dependency(principal)
    assert excinfo.value.status_code == 403


def test_authorize_target_allows_unrestricted_principal() -> None:
    principal = Principal(
        principal_id="p",
        display_name="P",
        scopes=frozenset({PrincipalScope.READ}),
        authentication_method=AuthenticationMethod.BEARER,
    )
    authorize_target(principal, "any-target")  # no raise


def test_authorize_target_enforces_explicit_allowlist() -> None:
    from fastapi import HTTPException

    principal = Principal(
        principal_id="p",
        display_name="P",
        scopes=frozenset({PrincipalScope.READ}),
        allowed_target_ids=frozenset({"dev-a"}),
        authentication_method=AuthenticationMethod.BEARER,
    )
    authorize_target(principal, "dev-a")  # no raise
    with pytest.raises(HTTPException) as excinfo:
        authorize_target(principal, "prod-b")
    assert excinfo.value.status_code == 403
