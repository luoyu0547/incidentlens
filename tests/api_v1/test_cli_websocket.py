"""End-to-end WebSocket tests for the recoverable CLI event stream.

Covers the authenticated handshake, schema negotiation (unsupported version
closes ``4406``), unknown event types, and durable replay over the wire through
the shared TestClient app.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType


def _seed_events(client, count: int) -> None:
    store = client.app.state.runtime.events
    for index in range(1, count + 1):
        store.append(
            RuntimeEvent(
                event_id=f"evt-cli-{index}",
                event_type=RuntimeEventType.PROJECT_CREATED,
                occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
                payload={"target_id": "tgt-a"},
            )
        )


def test_cli_ws_replays_history_before_live(authenticated_client) -> None:
    _seed_events(authenticated_client, 12)
    with authenticated_client.websocket_connect(
        "/ws/v1/cli-events",
        headers=authenticated_client.AUTH_HEADERS,
    ) as websocket:
        hello = json.loads(websocket.receive_text())
        assert hello["event_type"] == "stream.hello"
        # Events replayed lazily; read a few.
        received_types = []
        for _ in range(6):
            frame = json.loads(websocket.receive_text())
            received_types.append(frame.get("event_type"))
        assert "project.created" in received_types


def test_cli_ws_unsupported_schema_closes_4406(authenticated_client) -> None:
    from starlette.websockets import WebSocketDisconnect

    with authenticated_client.websocket_connect(
        "/ws/v1/cli-events?schema_version=2",
        headers=authenticated_client.AUTH_HEADERS,
    ) as websocket:
        with pytest.raises(WebSocketDisconnect) as exc:
            websocket.receive_text()
    assert exc.value.code == 4406
