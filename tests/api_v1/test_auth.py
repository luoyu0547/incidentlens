"""End-to-end authentication and CSRF tests for the v1 product API.

Covers: missing/invalid bearer tokens, session-cookie issuance and flags,
cookie GET without CSRF, cookie mutation rejection without ``X-CSRF-Token``,
bearer mutation without CSRF, target restrictions through the dependency stack,
and the structural rule that v1 request bodies never accept actor identity.
"""

from __future__ import annotations

from auth.helpers import AUTH_HEADERS, OPERATOR_A_PROFILE_ID
from fastapi.testclient import TestClient
from incidentlens_control_plane.auth.dependencies import (
    authorize_target,
    get_principal,
)
from incidentlens_control_plane.auth.types import Principal

SESSION_COOKIE = "incidentlens_session"


# ---------------------------------------------------------------------------
# Unauthenticated requests
# ---------------------------------------------------------------------------


def test_missing_bearer_is_rejected_with_401_envelope(auth_client: TestClient) -> None:
    response = auth_client.get("/api/v1/principal")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_invalid_bearer_is_rejected_with_401_envelope(
    auth_client: TestClient,
) -> None:
    response = auth_client.get(
        "/api/v1/principal",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_session_creation_requires_a_valid_token(auth_client: TestClient) -> None:
    response = auth_client.post("/api/v1/auth/session")
    assert response.status_code == 401

    response = auth_client.post(
        "/api/v1/auth/session",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401


def test_version_endpoint_stays_public(client: TestClient) -> None:
    response = client.get("/api/v1/version")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Session cookie issuance
# ---------------------------------------------------------------------------


def test_session_creation_sets_signed_cookie_and_returns_csrf(
    auth_client: TestClient,
) -> None:
    response = auth_client.post("/api/v1/auth/session", headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert SESSION_COOKIE in response.headers["set-cookie"]
    body = response.json()
    assert body["principal"]["principal_id"] == OPERATOR_A_PROFILE_ID
    assert len(body["csrf_token"]) == 32
    assert body["expires_at"] > 0


def test_session_cookie_has_protection_flags(auth_client: TestClient) -> None:
    response = auth_client.post("/api/v1/auth/session", headers=AUTH_HEADERS)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie


def test_secure_cookie_flag_follows_setting(tmp_path) -> None:
    import json as _json

    from auth.helpers import OPERATOR_A_TOKEN, OPERATOR_A_TOKEN_DIGEST
    from incidentlens_control_plane.config import RuntimeSettings
    from incidentlens_control_plane.main import create_app
    from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory

    profiles = _json.dumps(
        [
            {
                "principal_id": "op-secure",
                "display_name": "Op Secure",
                "scopes": ["read"],
                "token_digest": OPERATOR_A_TOKEN_DIGEST,
            }
        ]
    )
    app = create_app(
        RuntimeSettings(
            data_dir=tmp_path / "secure-data",
            auth_profiles_json=profiles,
            secure_cookies=True,
        ),
        transport_factory=FakeTransportFactory(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/session",
            headers={"Authorization": f"Bearer {OPERATOR_A_TOKEN}"},
        )
        assert response.status_code == 200
        assert "secure" in response.headers["set-cookie"].lower()


def test_session_creation_rejects_body_fields(auth_client: TestClient) -> None:
    response = auth_client.post(
        "/api/v1/auth/session",
        headers=AUTH_HEADERS,
        json={"token": "operator-a-bearer-token"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"


# ---------------------------------------------------------------------------
# Cookie-authenticated requests and CSRF
# ---------------------------------------------------------------------------


def test_cookie_get_without_csrf_is_allowed(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.get("/api/v1/principal")
    assert response.status_code == 200
    body = response.json()
    assert body["principal_id"] == OPERATOR_A_PROFILE_ID
    assert body["authentication_method"] == "session_cookie"


def test_cookie_mutation_without_csrf_is_rejected(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.post("/api/v1/auth/logout")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_cookie_mutation_with_csrf_succeeds(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": authenticated_client.csrf},
    )
    assert response.status_code == 204
    assert SESSION_COOKIE in response.headers["set-cookie"]


def test_cookie_mutation_with_wrong_csrf_is_rejected(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": "f" * 32},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_logout_ends_the_session(authenticated_client: TestClient) -> None:
    response = authenticated_client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": authenticated_client.csrf},
    )
    assert response.status_code == 204
    after = authenticated_client.get("/api/v1/principal")
    assert after.status_code == 401


# ---------------------------------------------------------------------------
# Bearer-authenticated requests
# ---------------------------------------------------------------------------


def test_bearer_get_returns_principal(auth_client: TestClient) -> None:
    response = auth_client.get("/api/v1/principal", headers=AUTH_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["principal_id"] == OPERATOR_A_PROFILE_ID
    assert body["authentication_method"] == "bearer"


def test_bearer_mutation_requires_no_csrf(auth_client: TestClient) -> None:
    response = auth_client.post("/api/v1/auth/logout", headers=AUTH_HEADERS)
    assert response.status_code == 204


# ---------------------------------------------------------------------------
# Body-actor rule and target restrictions
# ---------------------------------------------------------------------------


def test_body_actor_cannot_impersonate_principal(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": authenticated_client.csrf},
        json={"created_by": "admin"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"


def test_target_restriction_enforced_for_restricted_principal() -> None:
    import pytest
    from fastapi import HTTPException

    principal = Principal(
        principal_id="restricted",
        display_name="Restricted",
        scopes=frozenset({"read", "operate"}),
        allowed_target_ids=frozenset({"dev-a"}),
        authentication_method="bearer",
    )
    authorize_target(principal, "dev-a")  # allowed
    with pytest.raises(HTTPException) as excinfo:
        authorize_target(principal, "prod-b")
    assert excinfo.value.status_code == 403


def test_unrestricted_principal_can_address_any_target() -> None:
    principal = Principal(
        principal_id="broad",
        display_name="Broad",
        scopes=frozenset({"read"}),
        authentication_method="bearer",
    )
    authorize_target(principal, "anything")  # no raise


def test_get_principal_is_reusable_dependency(authenticated_client) -> None:
    """get_principal functions as a standalone FastAPI dependency."""
    from fastapi import Depends

    @authenticated_client.app.get("/api/v1/_test/auth-principal")
    async def _auth_principal(
        principal: Principal = Depends(get_principal),
    ) -> Principal:
        return principal

    response = authenticated_client.get("/api/v1/_test/auth-principal")
    assert response.status_code == 200
    assert response.json()["principal_id"] == OPERATOR_A_PROFILE_ID
    assert response.json()["authentication_method"] == "session_cookie"
