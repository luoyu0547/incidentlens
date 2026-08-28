"""Error-envelope and request-ID tests for the versioned product API."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.testclient import TestClient
from incidentlens_control_plane.api.errors import ApiProblem

# ---------------------------------------------------------------------------
# Request-ID propagation
# ---------------------------------------------------------------------------


def test_accepts_valid_inbound_request_id(client) -> None:
    response = client.get(
        "/api/v1/version", headers={"X-Request-ID": "c-123.abc_XYZ"}
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "c-123.abc_XYZ"


def test_replaces_invalid_inbound_request_id(client) -> None:
    response = client.get("/api/v1/version", headers={"X-Request-ID": "bad id!"})
    generated = response.headers["X-Request-ID"]
    assert generated.startswith("req_")
    assert generated != "bad id!"


def test_replaces_too_long_inbound_request_id(client) -> None:
    response = client.get("/api/v1/version", headers={"X-Request-ID": "x" * 81})
    assert response.headers["X-Request-ID"].startswith("req_")


# ---------------------------------------------------------------------------
# v1 exception normalization
# ---------------------------------------------------------------------------


def test_v1_http_exception_maps_to_stable_envelope(client) -> None:
    @client.app.get("/api/v1/_test/http-error")
    async def _http_error() -> None:
        raise HTTPException(status_code=404, detail="Nothing here")

    response = client.get("/api/v1/_test/http-error")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "resource_not_found"
    assert body["error"]["message"] == "Nothing here"
    assert body["error"]["request_id"].startswith("req_")
    assert response.headers["X-Request-ID"] == body["error"]["request_id"]


def test_v1_http_exception_unmapped_status_falls_back_to_http_code(client) -> None:
    @client.app.get("/api/v1/_test/teapot")
    async def _teapot() -> None:
        raise HTTPException(status_code=418, detail="Short and stout")

    response = client.get("/api/v1/_test/teapot")
    assert response.status_code == 418
    assert response.json()["error"]["code"] == "http_418"


def test_apiproblem_maps_to_stable_envelope(client) -> None:
    @client.app.get("/api/v1/_test/apiproblem")
    async def _apiproblem() -> None:
        raise ApiProblem(
            status_code=409,
            code="resource_conflict",
            message="Already exists",
            details={"project_id": "payments"},
        )

    response = client.get("/api/v1/_test/apiproblem")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "resource_conflict"
    assert body["error"]["message"] == "Already exists"
    assert body["error"]["details"] == {"project_id": "payments"}


def test_unhandled_exception_is_redacted_500(client) -> None:
    @client.app.get("/api/v1/_test/boom")
    async def _boom() -> None:
        raise RuntimeError("super-secret-root-cause")

    # ServerErrorMiddleware re-raises after sending; disable raise so we can
    # observe the response body Starlette produced.
    response = TestClient(client.app, raise_server_exceptions=False).get(
        "/api/v1/_test/boom"
    )
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == "Internal server error"
    assert body["error"]["request_id"].startswith("req_")
    assert "super-secret" not in response.text


# ---------------------------------------------------------------------------
# Legacy envelope compatibility
# ---------------------------------------------------------------------------


def test_legacy_http_exception_body_unchanged(client) -> None:
    response = client.get("/api/projects/not-a-real-project")
    assert response.status_code == 404
    assert response.json() == {"detail": "Project not found"}


def test_legacy_validation_body_unchanged(client) -> None:
    response = client.post("/api/projects", json={})
    assert response.status_code == 422
    body = response.json()
    assert isinstance(body.get("detail"), list)
    assert "error" not in body
