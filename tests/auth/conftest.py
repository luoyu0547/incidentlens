"""Shared fixtures for authentication tests.

``authenticated_client`` (and its bare ``auth_client`` sibling) are defined
here for the ``tests/auth`` package and mirrored in ``tests/api_v1/conftest.py``
for the HTTP-level auth tests.  Both import the same pure construction helpers
from :mod:`auth.helpers` so the profile/token/cookie configuration never drifts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth.helpers import AUTH_HEADERS, make_auth_app


@pytest.fixture
def auth_client(tmp_path: Path) -> TestClient:
    """A test client over an app with auth profiles but no session yet."""
    app = make_auth_app(tmp_path)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def authenticated_client(tmp_path: Path) -> TestClient:
    """An auth-enabled client with an established ``incidentlens_session``.

    The client carries the signed session cookie for subsequent requests, plus
    two useful attributes:

    - ``.csrf`` -- the CSRF nonce to echo via ``X-CSRF-Token`` on mutations.
    - ``.AUTH_HEADERS`` -- bearer headers that resolve to the same principal.
    """
    app = make_auth_app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/v1/auth/session", headers=AUTH_HEADERS)
        assert response.status_code == 200, response.text
        client.csrf = response.json()["csrf_token"]
        client.AUTH_HEADERS = dict(AUTH_HEADERS)
        yield client
