"""Contract tests for the versioned product API (``/api/v1``)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import create_app


def test_v1_version_has_stable_contract(client) -> None:
    response = client.get("/api/v1/version")
    assert response.status_code == 200
    assert response.json() == {
        "api_version": "v1",
        "stream_schema_versions": [1],
        "minimum_cli_protocol_version": "1.0.0",
        "minimum_web_protocol_version": "1.0.0",
    }
    assert response.headers["X-Request-ID"].startswith("req_")


def test_v1_validation_error_has_stable_envelope(client) -> None:
    response = client.get("/api/v1/version?unknown=x")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"


def test_v1_version_rejects_unknown_query_params(client) -> None:
    response = client.get("/api/v1/version?unknown=x&more=1")
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "request_validation_failed"
    assert body["error"]["request_id"].startswith("req_")


def test_openapi_exportable_offline_when_docs_disabled(client) -> None:
    schema = client.app.openapi()
    assert schema["info"]["title"] == "IncidentLens"
    assert schema["paths"]["/api/v1/version"]["get"]["operationId"] == "getApiVersion"


def test_docs_are_disabled_by_default(client) -> None:
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_expose_api_docs_mounts_docs(tmp_path: Path) -> None:
    app = create_app(
        RuntimeSettings(data_dir=tmp_path / "docs-enabled", expose_api_docs=True)
    )
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200
