"""Tests for the log subscription HTTP API and WebSocket replay/live dedupe."""

from __future__ import annotations

from datetime import UTC, datetime

from incidentlens_control_plane.logs.types import LogScope, LogSourceKind

from web.conftest import make_subscription_record


def test_create_subscription_requires_opt_in_true(client, registered_project) -> None:
    response = client.post(
        "/api/logs/subscriptions",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service_name": "payment-api",
            "source_kind": "docker",
            "scope": "container",
            "source_ref": "payments-api-1",
            "opt_in_streaming": False,
            "created_by": "alice",
        },
    )

    assert response.status_code == 400
    assert "opt_in_streaming=true" in response.text


def test_pause_resume_delete_subscription_state_machine(
    client, registered_project
) -> None:
    created = client.post(
        "/api/logs/subscriptions",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service_name": "payment-api",
            "source_kind": "docker",
            "scope": "container",
            "source_ref": "payments-api-1",
            "opt_in_streaming": True,
            "created_by": "alice",
        },
    )
    subscription_id = created.json()["subscription_id"]

    paused = client.post(f"/api/logs/subscriptions/{subscription_id}/pause")
    resumed = client.post(f"/api/logs/subscriptions/{subscription_id}/resume")
    deleted = client.delete(f"/api/logs/subscriptions/{subscription_id}")

    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"
    assert deleted.status_code == 204


def test_subscription_websocket_replays_then_streams_without_duplicate(
    client, runtime, registered_project
) -> None:
    subscription = runtime.log_store.create_subscription(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    record = make_subscription_record(subscription.subscription_id, "cursor-1")
    runtime.log_store.append_batch((record,))

    with client.websocket_connect(
        f"/api/logs/subscriptions/{subscription.subscription_id}/ws"
    ) as socket:
        replayed = socket.receive_json()
        runtime.subscriptions.publish_live_for_test(record)
        live = socket.receive_json()

    assert replayed["log_id"] == record.log_id
    assert live["event"] == "heartbeat"
