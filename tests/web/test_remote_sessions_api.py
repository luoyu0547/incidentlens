"""Tests for remote-session lifecycle HTTP API."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import create_app
from incidentlens_control_plane.remote_ops.transport import RemoteConnectionError


def test_connect_reuses_session_and_never_returns_credentials(
    client: TestClient, registered_project: str
) -> None:
    first = client.post(
        "/api/remote-sessions",
        json={"project_id": "payments", "target_id": "dev-a"},
    )
    second = client.post(
        "/api/remote-sessions",
        json={"project_id": "payments", "target_id": "dev-a"},
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["session_id"] == second.json()["session_id"]
    assert "credential" not in first.text
    assert "private" not in first.text


def test_container_session_is_fresh_and_parent_scoped(
    client: TestClient, connected_host: str
) -> None:
    body = {
        "project_id": "payments",
        "service": "payment-api",
        "container": "payments-api-1",
    }
    first = client.post(
        f"/api/remote-sessions/{connected_host}/containers", json=body
    )
    second = client.post(
        f"/api/remote-sessions/{connected_host}/containers", json=body
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["session_id"] != second.json()["session_id"]
    assert first.json()["parent_session_id"] == connected_host


def test_connect_unknown_project_or_target_returns_404(
    client: TestClient, registered_project: str
) -> None:
    unknown_project = client.post(
        "/api/remote-sessions",
        json={"project_id": "unknown", "target_id": "dev-a"},
    )
    assert unknown_project.status_code == 404

    unknown_target = client.post(
        "/api/remote-sessions",
        json={"project_id": "payments", "target_id": "missing"},
    )
    assert unknown_target.status_code == 404


def test_connect_failure_returns_redacted_502(tmp_path: Path) -> None:
    class FailingFactory:
        async def connect(self, target):
            raise RemoteConnectionError("host key verification failed: aa:bb:cc")

    app = create_app(
        RuntimeSettings(data_dir=tmp_path / "data"),
        transport_factory=FailingFactory(),
    )
    with TestClient(app) as client:
        source = (tmp_path / "src").resolve()
        response = client.post(
            "/api/projects",
            json={
                "project_id": "payments",
                "display_name": "Payments",
                "local_source_paths": [str(source)],
                "targets": [
                    {
                        "target_id": "dev-a",
                        "host": "dev-a.example.test",
                        "ssh_user": "deploy",
                    }
                ],
                "services": [],
            },
        )
        assert response.status_code == 201

        response = client.post(
            "/api/remote-sessions",
            json={"project_id": "payments", "target_id": "dev-a"},
        )
        assert response.status_code == 502
        assert "host key" not in response.text
        assert "verification failed" not in response.text
        assert "dev-a.example.test" not in response.text


def test_delete_session_is_idempotent(
    client: TestClient, connected_host: str
) -> None:
    first = client.delete(f"/api/remote-sessions/{connected_host}")
    second = client.delete(f"/api/remote-sessions/{connected_host}")
    assert first.status_code == 204
    assert second.status_code == 204


def test_delete_container_child_keeps_host_connected(
    client: TestClient, connected_host: str
) -> None:
    body = {
        "project_id": "payments",
        "service": "payment-api",
        "container": "payments-api-1",
    }
    child = client.post(
        f"/api/remote-sessions/{connected_host}/containers", json=body
    )
    assert child.status_code == 201
    child_id = child.json()["session_id"]

    deleted = client.delete(f"/api/remote-sessions/{child_id}")
    assert deleted.status_code == 204

    host_status = client.get(f"/api/remote-sessions/{connected_host}")
    assert host_status.status_code == 200
    assert host_status.json()["status"] in ("connected", "stale")


def test_session_status_shows_health_without_transport(
    client: TestClient, connected_host: str
) -> None:
    response = client.get(f"/api/remote-sessions/{connected_host}")
    assert response.status_code == 200
    assert response.json()["status"] in ("connected", "stale")
    assert "transport" not in response.text
    assert "ssh_user" not in response.text


def test_container_spawn_requires_registered_container(
    client: TestClient, connected_host: str
) -> None:
    body = {
        "project_id": "payments",
        "service": "payment-api",
        "container": "not-registered",
    }
    response = client.post(
        f"/api/remote-sessions/{connected_host}/containers", json=body
    )
    assert response.status_code == 409


def test_connect_request_rejects_unexpected_fields(
    client: TestClient, registered_project: str
) -> None:
    response = client.post(
        "/api/remote-sessions",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "host": "attacker.example.test",
            "ssh_user": "root",
            "allowed_paths": ["/etc"],
        },
    )
    assert response.status_code == 422
