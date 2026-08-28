"""Shared helpers for authentication tests.

This module is importable from both ``tests/auth`` and ``tests/api_v1``:
``tests/`` is pytest's source root (it has no ``__init__.py`` while its test
subdirectories do), so ``auth.helpers`` resolves to this module.

The fixture-facing functions are pure (no pytest dependency) so they can be
wired into any conftest or test without fixture plumbing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import create_app
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory

#: Known bearer token for the shared ``operator-a`` test profile.
OPERATOR_A_TOKEN = "operator-a-bearer-token"
OPERATOR_A_PROFILE_ID = "operator-a"
OPERATOR_A_DISPLAY_NAME = "Operator A"
OPERATOR_A_SCOPES = ["read", "operate", "approve", "admin"]

OPERATOR_A_TOKEN_DIGEST = hashlib.sha256(
    OPERATOR_A_TOKEN.encode("utf-8")
).hexdigest()

#: Deployment-profiles JSON matching the shared test profile.
AUTH_PROFILES_JSON = json.dumps(
    [
        {
            "principal_id": OPERATOR_A_PROFILE_ID,
            "display_name": OPERATOR_A_DISPLAY_NAME,
            "scopes": OPERATOR_A_SCOPES,
            "token_digest": OPERATOR_A_TOKEN_DIGEST,
        }
    ]
)

#: Bearer headers that act as ``operator-a``.
AUTH_HEADERS = {"Authorization": f"Bearer {OPERATOR_A_TOKEN}"}


def auth_settings(tmp_path: Path) -> RuntimeSettings:
    """Build runtime settings with the shared auth profile configured.

    ``secure_cookies`` is disabled so Starlette's TestClient can round-trip the
    signed session cookie over plain ``http://testserver`` (a Secure cookie is
    dropped by the HTTP client on non-HTTPS requests).  Production keeps the
    Secure flag on.
    """
    return RuntimeSettings(
        data_dir=tmp_path / "data",
        auth_profiles_json=AUTH_PROFILES_JSON,
        secure_cookies=False,
    )


def make_auth_app(tmp_path: Path):
    """Build the FastAPI app with auth profiles and a fake transport."""
    return create_app(
        auth_settings(tmp_path),
        transport_factory=FakeTransportFactory(),
    )


def establish_session(client: TestClient) -> str:
    """Mint a session cookie on *client* and return the CSRF nonce."""
    response = client.post("/api/v1/auth/session", headers=AUTH_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]
