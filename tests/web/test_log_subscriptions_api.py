"""Tests for the log subscription HTTP API and WebSocket replay/live dedupe."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import UTC, datetime
from pathlib import Path

import incidentlens_control_plane.main as main_module
import pytest
from fastapi import FastAPI
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.logs.types import LogScope, LogSourceKind
from incidentlens_control_plane.runtime import build_runtime

from web.conftest import make_subscription_record


def _subscription_payload() -> dict[str, object]:
    return {
        "project_id": "payments",
        "target_id": "dev-a",
        "service_name": "payment-api",
        "source_kind": "docker",
        "scope": "container",
        "source_ref": "payments-api-1",
        "opt_in_streaming": True,
        "created_by": "alice",
    }


def _await_live_subscriber_count(
    subscriptions, subscription_id: str, expected: int
) -> None:
    """Wait until the manager's live-queue registry reaches ``expected``."""
    for _ in range(500):
        if subscriptions.live_subscriber_count(subscription_id) == expected:
            return
        time.sleep(0.01)
    raise AssertionError(
        f"live subscriber count for {subscription_id} never reached {expected}"
    )


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
    created = client.post("/api/logs/subscriptions", json=_subscription_payload())
    subscription_id = created.json()["subscription_id"]

    paused = client.post(f"/api/logs/subscriptions/{subscription_id}/pause")
    resumed = client.post(f"/api/logs/subscriptions/{subscription_id}/resume")
    deleted = client.delete(f"/api/logs/subscriptions/{subscription_id}")

    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"
    assert deleted.status_code == 204


def test_pause_already_paused_subscription_returns_409(
    client, registered_project
) -> None:
    created = client.post("/api/logs/subscriptions", json=_subscription_payload())
    subscription_id = created.json()["subscription_id"]

    assert (
        client.post(f"/api/logs/subscriptions/{subscription_id}/pause").status_code
        == 200
    )
    second_pause = client.post(f"/api/logs/subscriptions/{subscription_id}/pause")

    assert second_pause.status_code == 409


def test_resume_active_subscription_returns_409(
    client, registered_project
) -> None:
    created = client.post("/api/logs/subscriptions", json=_subscription_payload())
    subscription_id = created.json()["subscription_id"]

    resumed = client.post(f"/api/logs/subscriptions/{subscription_id}/resume")

    assert resumed.status_code == 409


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


def test_websocket_disconnect_unregisters_live_queue(
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

    with client.websocket_connect(
        f"/api/logs/subscriptions/{subscription.subscription_id}/ws"
    ):
        _await_live_subscriber_count(
            runtime.subscriptions, subscription.subscription_id, 1
        )

    _await_live_subscriber_count(runtime.subscriptions, subscription.subscription_id, 0)


def test_lifespan_restores_subscriptions_before_requests_and_closes_before_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the real lifespan and assert startup/shutdown ordering.

    The lifespan must restore active log subscriptions before any request is
    served (``subscriptions.start``) and, on shutdown, close subscriptions
    before closing SSH sessions (``subscriptions.close`` then
    ``sessions.close``).
    """
    calls: list[str] = []

    class TrackingSubscriptions:
        async def start_active_opt_in(self) -> None:
            calls.append("subscriptions.start")

        async def close_all(self) -> None:
            calls.append("subscriptions.close")

    class TrackingSessions:
        async def close_all(self) -> None:
            calls.append("sessions.close")

    settings = RuntimeSettings(data_dir=tmp_path / "data")
    services = build_runtime(settings)
    # ``RuntimeServices`` is a frozen slots dataclass, so tracking stubs are
    # injected with ``dataclasses.replace`` rather than ``model_copy``.
    services = dataclasses.replace(
        services,
        subscriptions=TrackingSubscriptions(),  # type: ignore[arg-type]
        sessions=TrackingSessions(),  # type: ignore[arg-type]
    )

    # Inject the tracking-stub runtime into the REAL lifespan so startup and
    # shutdown drive our stubs instead of a second real runtime.
    monkeypatch.setattr(
        main_module,
        "build_runtime",
        lambda _settings, transport_factory=None: services,
    )

    app = FastAPI()

    async def drive() -> None:
        async with main_module._lifespan(app, settings):
            assert app.state.runtime is services
        assert app.state.runtime is None

    asyncio.run(drive())

    assert calls == ["subscriptions.start", "subscriptions.close", "sessions.close"]
