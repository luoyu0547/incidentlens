"""End-to-end SSE tests for the workspace invalidation stream.

These run against a real uvicorn server on a loopback port because the
in-process ASGI transports (Starlette TestClient and httpx.ASGITransport) both
buffer the entire response body, so a long-lived ``text/event-stream`` can only
be read incrementally over real HTTP.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import uvicorn
from auth.helpers import (
    AUTH_HEADERS,
    OPERATOR_A_DISPLAY_NAME,
    OPERATOR_A_PROFILE_ID,
    OPERATOR_A_SCOPES,
    OPERATOR_A_TOKEN_DIGEST,
)
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType
from incidentlens_control_plane.main import create_app
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory

#: Bearer token for a read-only principal restricted to ``tgt-a``.
RESTRICTED_TOKEN = "restricted-operator-bearer-token"
RESTRICTED_AUTH_HEADERS = {"Authorization": f"Bearer {RESTRICTED_TOKEN}"}

_PROFILES = [
    {
        "principal_id": OPERATOR_A_PROFILE_ID,
        "display_name": OPERATOR_A_DISPLAY_NAME,
        "scopes": list(OPERATOR_A_SCOPES),
        "token_digest": OPERATOR_A_TOKEN_DIGEST,
    },
    {
        "principal_id": "restricted-operator",
        "display_name": "Restricted Operator",
        "scopes": ["read"],
        "allowed_target_ids": ["tgt-a"],
        "token_digest": hashlib.sha256(RESTRICTED_TOKEN.encode("utf-8")).hexdigest(),
    },
]
PROFILES_JSON = json.dumps(_PROFILES)


@dataclass
class SseServer:
    """Running uvicorn instance plus the app/settings needed by the tests."""

    base_url: str
    app: Any
    settings: RuntimeSettings


@dataclass
class SseFrame:
    """One parsed SSE event block."""

    event: str | None = None
    id: str | None = None
    data: dict[str, object] | None = None
    comment: str | None = None


@pytest.fixture(scope="module")
def sse_server(tmp_path_factory: pytest.TempPathFactory) -> SseServer:
    """Start uvicorn on a loopback port and yield the running server."""
    data_dir = tmp_path_factory.mktemp("sse-server") / "data"
    settings = RuntimeSettings(
        data_dir=data_dir,
        auth_profiles_json=PROFILES_JSON,
        secure_cookies=False,
    )
    app = create_app(settings, transport_factory=FakeTransportFactory())
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started and server.servers:
                port = server.servers[0].sockets[0].getsockname()[1]
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("uvicorn failed to start")
        for _ in range(200):
            if app.state.runtime is not None:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("runtime did not initialize")
        yield SseServer(
            base_url=f"http://127.0.0.1:{port}", app=app, settings=settings
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _clean_events(sse_server: SseServer) -> None:
    """Wipe durable events after every test so each test starts clean."""
    yield
    with sqlite3.connect(sse_server.settings.data_dir / "runtime.db") as connection:
        connection.execute("DELETE FROM runtime_events")
        connection.commit()


@pytest.fixture
def sse_client(sse_server: SseServer) -> httpx.Client:
    """A fresh loopback HTTP client for one test."""
    with httpx.Client(base_url=sse_server.base_url, timeout=15) as client:
        yield client


def _store(sse_server: SseServer):
    return sse_server.app.state.runtime.events


def _seed_event(
    sse_server: SseServer,
    *,
    event_id: str,
    event_type: RuntimeEventType,
    target_id: str | None = None,
    investigation_id: str | None = None,
    service_name: str | None = None,
    **extra: object,
) -> RuntimeEvent:
    payload: dict[str, object] = dict(extra)
    if target_id is not None:
        payload["target_id"] = target_id
    if investigation_id is not None:
        payload["investigation_id"] = investigation_id
    if service_name is not None:
        payload["service_name"] = service_name
    return _store(sse_server).append(
        RuntimeEvent(
            event_id=event_id,
            event_type=event_type,
            occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
            payload=payload,
        )
    )


@pytest.fixture
def seeded_events(sse_server: SseServer) -> list[RuntimeEvent]:
    """Seed five mapped investigation events and return the stored records."""
    return [
        _seed_event(
            sse_server,
            event_id=f"evt-ws-{index}",
            event_type=RuntimeEventType.INVESTIGATION_STATUS_CHANGED,
            target_id="tgt-a",
            investigation_id=f"inv-{index}",
        )
        for index in range(1, 6)
    ]


def read_sse_frames(
    response: httpx.Response, count: int | None
) -> list[SseFrame]:
    """Read ``count`` SSE frames (or all frames until EOF when ``None``)."""
    frames: list[SseFrame] = []
    event: str | None = None
    event_id: str | None = None
    data_lines: list[str] = []
    comment: str | None = None

    for line in response.iter_lines():
        if line == "":
            if (
                event is not None
                or event_id is not None
                or data_lines
                or comment is not None
            ):
                data = json.loads("\n".join(data_lines)) if data_lines else None
                frames.append(
                    SseFrame(event=event, id=event_id, data=data, comment=comment)
                )
                event = None
                event_id = None
                data_lines = []
                comment = None
                if count is not None and len(frames) >= count:
                    break
        elif line.startswith(":"):
            comment = line[1:].strip()
        elif line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("id:"):
            event_id = line[len("id:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
    return frames


def test_workspace_sse_replays_after_last_event_id(
    sse_client: httpx.Client, seeded_events: list[RuntimeEvent]
) -> None:
    with sse_client.stream(
        "GET",
        "/events/v1/workspace",
        headers={**AUTH_HEADERS, "Last-Event-ID": seeded_events[2].event_id},
    ) as response:
        frames = read_sse_frames(response, count=2)
    assert response.status_code == 200
    assert [frame.id for frame in frames] == [
        seeded_events[3].event_id,
        seeded_events[4].event_id,
    ]
    assert all(frame.event == "resource.changed" for frame in frames)


def test_workspace_sse_query_after_event_id_fallback(
    sse_client: httpx.Client, seeded_events: list[RuntimeEvent]
) -> None:
    with sse_client.stream(
        "GET",
        f"/events/v1/workspace?after_event_id={seeded_events[0].event_id}",
        headers=AUTH_HEADERS,
    ) as response:
        frames = read_sse_frames(response, count=2)
    assert [frame.id for frame in frames] == [
        seeded_events[1].event_id,
        seeded_events[2].event_id,
    ]


def test_workspace_sse_last_event_id_header_wins_over_query(
    sse_client: httpx.Client, seeded_events: list[RuntimeEvent]
) -> None:
    with sse_client.stream(
        "GET",
        f"/events/v1/workspace?after_event_id={seeded_events[0].event_id}",
        headers={**AUTH_HEADERS, "Last-Event-ID": seeded_events[2].event_id},
    ) as response:
        frames = read_sse_frames(response, count=2)
    assert [frame.id for frame in frames] == [
        seeded_events[3].event_id,
        seeded_events[4].event_id,
    ]


def test_workspace_sse_bearer_auth(
    sse_client: httpx.Client, seeded_events: list[RuntimeEvent]
) -> None:
    with sse_client.stream(
        "GET", "/events/v1/workspace", headers=AUTH_HEADERS
    ) as response:
        frames = read_sse_frames(response, count=1)
    assert response.status_code == 200
    assert frames[0].event == "resource.changed"


def test_workspace_sse_cookie_auth(
    sse_client: httpx.Client, seeded_events: list[RuntimeEvent]
) -> None:
    response = sse_client.post("/api/v1/auth/session", headers=AUTH_HEADERS)
    assert response.status_code == 200
    with sse_client.stream("GET", "/events/v1/workspace") as response:
        frames = read_sse_frames(response, count=1)
    assert response.status_code == 200
    assert frames[0].event == "resource.changed"


def test_workspace_sse_requires_auth(sse_server: SseServer) -> None:
    with httpx.Client(base_url=sse_server.base_url, timeout=15) as client:
        response = client.get("/events/v1/workspace")
    assert response.status_code == 401


def test_workspace_sse_target_filter_query(
    sse_server: SseServer, sse_client: httpx.Client
) -> None:
    _seed_event(
        sse_server,
        event_id="evt-a-1",
        event_type=RuntimeEventType.INVESTIGATION_CREATED,
        target_id="tgt-a",
        investigation_id="inv-a-1",
    )
    _seed_event(
        sse_server,
        event_id="evt-b-1",
        event_type=RuntimeEventType.INVESTIGATION_CREATED,
        target_id="tgt-b",
        investigation_id="inv-b-1",
    )
    with sse_client.stream(
        "GET", "/events/v1/workspace?target_id=tgt-a", headers=AUTH_HEADERS
    ) as response:
        frames = read_sse_frames(response, count=1)
    assert response.status_code == 200
    assert frames[0].data["target_id"] == "tgt-a"
    assert frames[0].data["resource_id"] == "inv-a-1"


def test_workspace_sse_restricted_principal_filters_targets(
    sse_server: SseServer, sse_client: httpx.Client
) -> None:
    _seed_event(
        sse_server,
        event_id="evt-r-a",
        event_type=RuntimeEventType.INVESTIGATION_CREATED,
        target_id="tgt-a",
        investigation_id="inv-r-a",
    )
    _seed_event(
        sse_server,
        event_id="evt-r-b",
        event_type=RuntimeEventType.INVESTIGATION_CREATED,
        target_id="tgt-b",
        investigation_id="inv-r-b",
    )
    with sse_client.stream(
        "GET", "/events/v1/workspace", headers=RESTRICTED_AUTH_HEADERS
    ) as response:
        frames = read_sse_frames(response, count=1)
    assert response.status_code == 200
    assert frames[0].data["target_id"] == "tgt-a"


def test_workspace_sse_restricted_principal_rejects_foreign_target(
    sse_server: SseServer, sse_client: httpx.Client
) -> None:
    with sse_client.stream(
        "GET", "/events/v1/workspace?target_id=tgt-b", headers=RESTRICTED_AUTH_HEADERS
    ) as response:
        assert response.status_code == 403


def test_workspace_sse_unknown_cursor_emits_gap_and_closes(
    sse_client: httpx.Client, seeded_events: list[RuntimeEvent]
) -> None:
    with sse_client.stream(
        "GET",
        "/events/v1/workspace",
        headers={**AUTH_HEADERS, "Last-Event-ID": "evt-does-not-exist"},
    ) as response:
        frames = read_sse_frames(response, count=None)
    assert response.status_code == 200
    assert len(frames) == 1
    assert frames[0].event == "stream.gap"
    assert frames[0].id == "evt-does-not-exist"
    assert frames[0].data["event_type"] == "stream.gap"
    assert frames[0].data["action"] == "reload_snapshot"
    assert frames[0].data["reason"]


def test_workspace_sse_does_not_forward_payload(
    sse_server: SseServer, sse_client: httpx.Client
) -> None:
    _seed_event(
        sse_server,
        event_id="evt-secret",
        event_type=RuntimeEventType.APPROVAL_REQUESTED,
        target_id="tgt-a",
        api_key="super-secret",
        password="hunter2",
        backup_plaintext="TOP SECRET",
    )
    with sse_client.stream(
        "GET", "/events/v1/workspace", headers=AUTH_HEADERS
    ) as response:
        frames = read_sse_frames(response, count=1)
    rendered = json.dumps(frames[0].data)
    assert "super-secret" not in rendered
    assert "hunter2" not in rendered
    assert "TOP SECRET" not in rendered
    assert set(frames[0].data) == {
        "schema_version",
        "event_id",
        "event_type",
        "occurred_at",
        "resource_kind",
        "resource_id",
        "target_id",
        "service_id",
    }


def test_workspace_sse_maps_resource_fields(
    sse_server: SseServer, sse_client: httpx.Client
) -> None:
    _seed_event(
        sse_server,
        event_id="evt-inv",
        event_type=RuntimeEventType.INVESTIGATION_STARTED,
        target_id="tgt-a",
        investigation_id="inv-9",
    )
    _seed_event(
        sse_server,
        event_id="evt-log",
        event_type=RuntimeEventType.LOG_SUBSCRIPTION_STARTED,
        target_id="tgt-a",
        service_name="payment-api",
    )
    _seed_event(
        sse_server,
        event_id="evt-project",
        event_type=RuntimeEventType.PROJECT_CREATED,
    )
    _seed_event(
        sse_server,
        event_id="evt-evidence",
        event_type=RuntimeEventType.EVIDENCE_APPENDED,
        investigation_id="inv-9",
    )
    with sse_client.stream(
        "GET", "/events/v1/workspace", headers=AUTH_HEADERS
    ) as response:
        frames = read_sse_frames(response, count=4)
    by_id = {frame.id: frame.data for frame in frames}
    assert by_id["evt-inv"]["resource_kind"] == "investigation"
    assert by_id["evt-inv"]["resource_id"] == "inv-9"
    assert by_id["evt-inv"]["target_id"] == "tgt-a"
    assert by_id["evt-inv"]["service_id"] is None
    assert by_id["evt-log"]["resource_kind"] == "service"
    assert by_id["evt-log"]["resource_id"] == "payment-api"
    assert by_id["evt-log"]["service_id"] == "payment-api"
    assert by_id["evt-project"]["resource_kind"] == "overview"
    assert by_id["evt-evidence"]["resource_kind"] == "evidence"
    assert by_id["evt-evidence"]["resource_id"] == "inv-9"


def test_workspace_sse_sets_streaming_headers(
    sse_client: httpx.Client, seeded_events: list[RuntimeEvent]
) -> None:
    with sse_client.stream(
        "GET", "/events/v1/workspace", headers=AUTH_HEADERS
    ) as response:
        frames = read_sse_frames(response, count=1)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "no-cache" in response.headers["cache-control"]
    assert "no-transform" in response.headers["cache-control"]
    assert response.headers["x-accel-buffering"] == "no"
    assert frames[0].event == "resource.changed"


def test_workspace_sse_disconnect_closes_stream_cleanly(
    sse_client: httpx.Client, seeded_events: list[RuntimeEvent]
) -> None:
    with sse_client.stream(
        "GET", "/events/v1/workspace", headers=AUTH_HEADERS
    ) as response:
        frames = read_sse_frames(response, count=1)
    assert frames[0].event == "resource.changed"
    # Exiting the context above dropped the connection; the server must not
    # have errored. A follow-up connection proves the app is still healthy.
    health = sse_client.get("/healthz")
    assert health.status_code == 200
