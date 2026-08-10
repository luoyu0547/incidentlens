"""Tests for project registry HTTP API."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Create a test client with isolated data directory."""
    app = create_app(RuntimeSettings(data_dir=tmp_path / "data"))
    with TestClient(app) as client:
        yield client


@pytest.fixture
def payload(tmp_path: Path) -> dict[str, object]:
    """Sample project registration payload."""
    return {
        "project_id": "payments",
        "display_name": "Payments",
        "local_source_paths": [str((tmp_path / "src").resolve())],
        "targets": [
            {
                "target_id": "dev-a",
                "host": "dev-a.example.test",
                "ssh_user": "deploy",
                "ssh_config_alias": "dev-a",
            }
        ],
        "services": [
            {
                "compose_service": "payment-api",
                "container_names": [],
                "local_source_path": str((tmp_path / "src").resolve()),
                "container_path_hints": ["/app"],
                "allowed_log_paths": ["/var/log/payment/*.log"],
            }
        ],
    }


def test_project_crud_persists_and_emits_events(
    client: TestClient, payload: dict[str, object]
) -> None:
    """Test project CRUD operations and event emission."""
    # Create project
    created = client.post("/api/projects", json=payload)
    assert created.status_code == 201

    # Get project
    retrieved = client.get("/api/projects/payments")
    assert retrieved.status_code == 200
    assert retrieved.json()["display_name"] == "Payments"

    # List projects
    projects = client.get("/api/projects")
    assert projects.status_code == 200
    assert len(projects.json()) == 1

    # Check events
    events = client.get("/api/events", params={"after": 0})
    assert events.status_code == 200
    assert events.json()[0]["event_type"] == "project.created"


def test_project_api_maps_conflicts_and_missing_records(
    client: TestClient, payload: dict[str, object]
) -> None:
    """Test API error handling for conflicts and missing records."""
    # Create project
    assert client.post("/api/projects", json=payload).status_code == 201

    # Duplicate creation
    assert client.post("/api/projects", json=payload).status_code == 409

    # Missing project
    assert client.get("/api/projects/unknown").status_code == 404

    # Update missing project
    assert client.put("/api/projects/unknown", json=payload).status_code == 409

    # Delete missing project
    assert client.delete("/api/projects/unknown").status_code == 404


def test_project_api_rejects_relative_source_path(
    client: TestClient, payload: dict[str, object]
) -> None:
    """Test API rejects relative source paths."""
    invalid = {**payload, "local_source_paths": ["relative/source"]}
    assert client.post("/api/projects", json=invalid).status_code == 422


def test_project_api_put_matching_id_returns_404_when_missing(
    client: TestClient, payload: dict[str, object]
) -> None:
    # Create the project first
    assert client.post("/api/projects", json=payload).status_code == 201

    # Now try to PUT a different project_id that matches URL and body
    unknown_payload = {**payload, "project_id": "unknown"}
    assert client.put("/api/projects/unknown", json=unknown_payload).status_code == 404


def test_project_api_list_is_sorted_by_project_id(client: TestClient) -> None:
    # Create projects in non-alphabetical order
    for pid, name in [("zebra", "Zebra"), ("alpha", "Alpha"), ("middle", "Middle")]:
        client.post(
            "/api/projects",
            json={
                "project_id": pid,
                "display_name": name,
                "local_source_paths": [],
                "targets": [],
                "services": [],
            },
        )

    projects = client.get("/api/projects").json()
    ids = [p["project_id"] for p in projects]
    assert ids == sorted(ids)  # Should be alpha, middle, zebra
