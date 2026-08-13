"""Tests for event stream HTTP API."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.investigation.fake_provider import StopStep
from incidentlens_control_plane.investigation.provider import Conclusion, StopSignal
from incidentlens_control_plane.investigation.state_machine import AgentRunStatus
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    EvidenceReference,
    StopReason,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope
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


# ---------------------------------------------------------------------------
# Investigation events flow through the same shared stream
# ---------------------------------------------------------------------------


def _start_scripted_investigation(client: TestClient, runtime) -> str:
    """Create an investigation, script its parent run, and start it."""
    now = datetime.now(UTC)
    created = client.post(
        "/api/investigations",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service": "payment-api",
            "symptom": "checkout requests are failing",
        },
    )
    assert created.status_code == 201
    inv_id = created.json()["investigation_id"]
    investigation = runtime.investigation_store.get_investigation(inv_id)

    run = AgentRun(
        agent_run_id="run-evt-1",
        investigation_id=inv_id,
        parent_run_id=None,
        kind=AgentRunKind.PARENT,
        scope=AgentScope(project_id="payments", target_id="dev-a", scope=LogScope.HOST),
        status=AgentRunStatus.CREATED,
        budget=AgentBudget(),
        usage=UsageCounters(),
        created_at=now,
        updated_at=now,
    )
    runtime.investigation_store.create_agent_run(run)
    ref = runtime.evidence_service.record_validation_result(
        agent_run_id="run-evt-1",
        incident_id=investigation.incident_id,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_ref="seed",
        validator="test",
        passed=True,
        detail="seed evidence",
        created_by="test",
        now=now,
    )
    run = run.model_copy(
        update={
            "evidence": (
                EvidenceReference(
                    evidence_id=ref.evidence_ref_id,
                    operation_id="seed",
                    summary="seed evidence",
                ),
            )
        }
    )
    runtime.investigation_store.update_agent_run(run)
    runtime.fake_provider.set_script(
        "run-evt-1",
        [
            StopStep(
                stop_signal=StopSignal(
                    stop_reason=StopReason.COMPLETED, summary="complete"
                ),
                conclusion=Conclusion(
                    summary="root cause identified",
                    evidence_ids=(ref.evidence_ref_id,),
                ),
            )
        ],
    )
    started = client.post(
        f"/api/investigations/{inv_id}/start",
        json={
            "scope": {
                "project_id": "payments",
                "target_id": "dev-a",
                "scope": "host",
            }
        },
    )
    assert started.status_code == 200
    return inv_id


def test_investigation_events_flow_through_shared_stream(
    client: TestClient,
) -> None:
    """Create/start an investigation and observe its events on /api/events."""
    runtime = client.app.state.runtime
    inv_id = _start_scripted_investigation(client, runtime)

    events = client.get("/api/events", params={"after": 0}).json()
    types = [event["event_type"] for event in events]
    assert "investigation.created" in types
    assert "investigation.started" in types
    assert "agent_run.started" in types
    assert "agent_run.status_changed" in types
    assert "investigation.status_changed" in types

    created = next(
        event for event in events if event["event_type"] == "investigation.created"
    )
    assert created["payload"]["investigation_id"] == inv_id
    assert created["payload"]["status"] == "created"
    # Payloads carry IDs/status/counts only — no symptom, host or raw content.
    assert "symptom" not in created["payload"]
    assert "host" not in created["payload"]

    run_events = [
        event
        for event in events
        if event["event_type"].startswith("agent_run.")
    ]
    assert all("run_id" in event["payload"] for event in run_events)
    assert all("investigation_id" in event["payload"] for event in run_events)


def test_investigation_events_stream_on_websocket(client: TestClient) -> None:
    """The live WS stream carries investigation.created as a live event."""
    with client.websocket_connect("/api/events/ws?after=0") as socket:
        created = client.post(
            "/api/investigations",
            json={
                "project_id": "payments",
                "target_id": "dev-a",
                "service": "payment-api",
                "symptom": "checkout requests are failing",
            },
        )
        assert created.status_code == 201
        inv_id = created.json()["investigation_id"]

        live = socket.receive_json()
        assert live["event_type"] == "investigation.created"
        assert live["payload"]["investigation_id"] == inv_id
