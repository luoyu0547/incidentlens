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

import json
from pathlib import Path
from typing import Annotated

import pytest
from auth.helpers import AUTH_HEADERS, make_auth_app
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from incidentlens_control_plane.api.idempotency import (
    IdempotencyInProgressError,
    execute_idempotent,
    idempotency_request_sha256,
    in_progress_response,
)
from incidentlens_control_plane.auth.dependencies import get_principal
from incidentlens_control_plane.auth.types import Principal
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import create_app
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
from pydantic import BaseModel, ConfigDict


class TestIdempotentBody(BaseModel):
    """Strict request body accepted by the TEST-ONLY fixture route."""

    model_config = ConfigDict(extra="forbid")

    value: str


class TestIdempotentResult(BaseModel):
    """Response body created (and later exactly replayed) by the fixture route."""

    value: str
    created: bool


def _register_test_idempotent_route(app: FastAPI) -> None:
    """Mount ``POST /api/v1/test-idempotent`` on a test app only.

    The route lives here (not in the product API) because the idempotency
    plan intentionally has no product route yet: this fixture proves
    ``execute_idempotent`` end-to-end through the real auth dependency stack,
    strict body validation, and the stable v1 error envelope.
    """

    @app.post("/api/v1/test-idempotent", status_code=201, tags=["_test"])
    async def test_idempotent(
        request: Request,
        body: TestIdempotentBody,
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> JSONResponse:
        service = request.app.state.runtime.idempotency
        route_key = request.url.path
        canonical_body = json.dumps(
            body.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        request_sha256 = idempotency_request_sha256(
            method="POST",
            route_key=route_key,
            path_params={},
            canonical_body=canonical_body,
        )

        async def action() -> tuple[int, TestIdempotentResult]:
            return 201, TestIdempotentResult(value=body.value, created=True)

        try:
            status_code, payload, replayed = await execute_idempotent(
                service=service,
                principal=principal,
                method="POST",
                route_key=route_key,
                idempotency_key=request.headers.get("Idempotency-Key"),
                request_sha256=request_sha256,
                response_type=TestIdempotentResult,
                action=action,
            )
        except IdempotencyInProgressError:
            return in_progress_response(request)
        headers = {"Idempotency-Replayed": "true"} if replayed else None
        return JSONResponse(
            status_code=status_code,
            content=payload.model_dump(mode="json"),
            headers=headers,
        )


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
    ``.AUTH_HEADERS`` (bearer headers resolving to the same principal).  The
    TEST-ONLY idempotency fixture route is mounted on the same app.
    """
    app = make_auth_app(tmp_path)
    _register_test_idempotent_route(app)
    with TestClient(app) as client:
        response = client.post("/api/v1/auth/session", headers=AUTH_HEADERS)
        assert response.status_code == 200, response.text
        client.csrf = response.json()["csrf_token"]
        client.AUTH_HEADERS = dict(AUTH_HEADERS)
        yield client
