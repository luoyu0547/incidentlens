"""Shared fixtures for versioned product API tests.

The ``client`` fixture follows ``tests/web/conftest.py``: the real FastAPI
application built with a fake remote transport factory so no request touches
the network.

``authenticated_client`` (and its bare ``auth_client`` sibling) build the app
with the shared ``operator-a`` deployment profile configured.  They are meant
for authentication tests and later tasks that exercise the protected v1
surface, and share construction logic with ``tests/auth/conftest.py`` through
``auth.helpers``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from auth.helpers import AUTH_HEADERS, make_auth_app
from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import create_app
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Create a test client whose session manager uses a fake transport."""
    app = create_app(
        RuntimeSettings(data_dir=tmp_path / "data"),
        transport_factory=FakeTransportFactory(),
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def auth_client(tmp_path: Path) -> TestClient:
    """A client over an app with auth profiles configured but no session yet."""
    app = make_auth_app(tmp_path)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def authenticated_client(tmp_path: Path) -> TestClient:
    """An auth-enabled client with an established ``incidentlens_session``.

    The client carries the signed session cookie for subsequent requests, plus
    ``.csrf`` (the CSRF nonce to echo via ``X-CSRF-Token`` on mutations) and
    ``.AUTH_HEADERS`` (bearer headers resolving to the same principal).
    """
    app = make_auth_app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/v1/auth/session", headers=AUTH_HEADERS)
        assert response.status_code == 200, response.text
        client.csrf = response.json()["csrf_token"]
        client.AUTH_HEADERS = dict(AUTH_HEADERS)
        yield client
