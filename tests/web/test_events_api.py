"""Tests for event stream HTTP API."""

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


def test_websocket_replays_then_streams_project_events(
    client: TestClient,
) -> None:
    """Test WebSocket replays historical events then streams live events."""
    payload = {
        "project_id": "payments",
        "display_name": "Payments",
        "local_source_paths": [],
        "targets": [],
        "services": [],
    }

    # Create a project first
    client.post("/api/projects", json=payload)

    # Connect to WebSocket
    with client.websocket_connect("/api/events/ws?after=0") as socket:
        # Should receive the project.created event
        replayed = socket.receive_json()
        assert replayed["event_type"] == "project.created"

        # Update the project
        client.put(
            "/api/projects/payments",
            json={**payload, "display_name": "Payments API"},
        )

        # Should receive the project.updated event
        live = socket.receive_json()
        assert live["event_type"] == "project.updated"
        assert live["sequence"] > replayed["sequence"]
