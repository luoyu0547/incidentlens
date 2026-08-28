"""Unit tests for the workspace invalidation stream.

These drive :class:`WorkspaceEventStream` directly against a real SQLite event
store and in-memory broker, capturing the SSE frame text so the replay-to-live
handoff, event-ID cursor resolution, gap detection, target authorization,
heartbeat and disconnect cleanup are asserted without an HTTP stack.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType
from incidentlens_control_plane.streams.workspace import WorkspaceEventStream


def _store(tmp_path: Path) -> RuntimeEventStore:
    store = RuntimeEventStore(lambda: sqlite3.connect(tmp_path / "events.db"))
    store.migrate()
    return store


def _append(
    store: RuntimeEventStore,
    *,
    event_id: str,
    event_type: RuntimeEventType,
    target_id: str | None = None,
    investigation_id: str | None = None,
    service_name: str | None = None,
    service: str | None = None,
    **extra: object,
) -> RuntimeEvent:
    payload: dict[str, object] = dict(extra)
    if target_id is not None:
        payload["target_id"] = target_id
    if investigation_id is not None:
        payload["investigation_id"] = investigation_id
    if service_name is not None:
        payload["service_name"] = service_name
    if service is not None:
        payload["service"] = service
    return store.append(
        RuntimeEvent(
            event_id=event_id,
            event_type=event_type,
            occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
            payload=payload,
        )
    )


def _seed(
    store: RuntimeEventStore,
    count: int,
    *,
    target_id: str = "tgt-a",
    event_type: RuntimeEventType = RuntimeEventType.INVESTIGATION_STATUS_CHANGED,
    prefix: str = "ws",
) -> list[RuntimeEvent]:
    return [
        _append(
            store,
            event_id=f"evt-{prefix}-{index}",
            event_type=event_type,
            target_id=target_id,
            investigation_id=f"inv-{prefix}-{index}",
        )
        for index in range(1, count + 1)
    ]


def _frame_event(frame: str) -> str | None:
    for line in frame.splitlines():
        if line.startswith("event:"):
            return line[len("event:") :].strip()
    return None


def _frame_id(frame: str) -> str | None:
    for line in frame.splitlines():
        if line.startswith("id:"):
            return line[len("id:") :].strip()
    return None


def _frame_data(frame: str) -> dict[str, object]:
    data_lines = [
        line[len("data:") :].strip()
        for line in frame.splitlines()
        if line.startswith("data:")
    ]
    return json.loads("\n".join(data_lines))


def _changed_frames(frames: list[str]) -> list[dict[str, object]]:
    return [
        _frame_data(frame)
        for frame in frames
        if _frame_event(frame) == "resource.changed"
    ]


def _is_heartbeat(frame: str) -> bool:
    return frame.startswith(": heartbeat ")


async def _consume(frames: list[str], generator: AsyncIterator[str]) -> None:
    try:
        async for frame in generator:
            frames.append(frame)
    finally:
        await generator.aclose()


def _run_stream(
    store: RuntimeEventStore,
    broker: RuntimeEventBroker,
    *,
    after_event_id: str | None = None,
    target_id: str | None = None,
    allowed_target_ids: frozenset[str] | None = None,
    heartbeat_seconds: float = 1.0,
    settle_seconds: float = 0.2,
) -> list[str]:
    frames: list[str] = []

    async def scenario() -> None:
        stream = WorkspaceEventStream(
            events=store, broker=broker, heartbeat_seconds=heartbeat_seconds
        )
        generator = stream.run(
            after_event_id=after_event_id,
            target_id=target_id,
            allowed_target_ids=allowed_target_ids,
        )
        task = asyncio.create_task(_consume(frames, generator))
        await asyncio.sleep(settle_seconds)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    asyncio.run(scenario())
    return frames


def test_workspace_stream_replays_all_relevant_invalidations(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=16)
    seeded = _seed(store, 5)

    frames = [f for f in _run_stream(store, broker) if _frame_event(f) == "resource.changed"]
    assert [_frame_data(f)["event_id"] for f in frames] == [
        event.event_id for event in seeded
    ]
    assert all(_frame_data(f)["resource_kind"] == "investigation" for f in frames)


def test_workspace_stream_resumes_after_event_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=16)
    seeded = _seed(store, 5)

    frames = _run_stream(store, broker, after_event_id=seeded[1].event_id)
    changed = _changed_frames(frames)
    assert [frame["event_id"] for frame in changed] == [
        event.event_id for event in seeded[2:]
    ]


def test_workspace_stream_replays_1501_events_without_gap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=16)
    _seed(store, 1501)

    changed = _changed_frames(
        _run_stream(store, broker, settle_seconds=0.5)
    )
    assert len(changed) == 1501
    assert [frame["event_id"] for frame in changed] == [
        f"evt-ws-{index}" for index in range(1, 1502)
    ]


def test_workspace_stream_fresh_connect_on_empty_history_is_silent(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)

    frames = _run_stream(store, broker)
    assert frames == []


def test_workspace_stream_filters_by_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    _seed(store, 3, target_id="tgt-a", prefix="a")
    _seed(store, 3, target_id="tgt-b", prefix="b")

    frames = _run_stream(store, broker, target_id="tgt-a")
    changed = _changed_frames(frames)
    assert len(changed) == 3
    assert {frame["target_id"] for frame in changed} == {"tgt-a"}


def test_workspace_stream_filters_unauthorized_targets(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    _seed(store, 3, target_id="tgt-a", prefix="a")
    _seed(store, 2, target_id="tgt-b", prefix="b")

    frames = _run_stream(
        store, broker, allowed_target_ids=frozenset({"tgt-a"})
    )
    changed = _changed_frames(frames)
    assert {frame["target_id"] for frame in changed} == {"tgt-a"}


def test_workspace_stream_unknown_cursor_emits_gap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    _seed(store, 3)

    frames = _run_stream(store, broker, after_event_id="evt-does-not-exist")
    assert [_frame_event(frame) for frame in frames] == ["stream.gap"]
    gap = _frame_data(frames[0])
    assert gap["event_type"] == "stream.gap"
    assert gap["action"] == "reload_snapshot"
    assert gap["event_id"] == "evt-does-not-exist"
    assert gap["reason"]


def test_workspace_stream_pruned_cursor_emits_gap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    seeded = _seed(store, 5)
    with sqlite3.connect(tmp_path / "events.db") as connection:
        connection.execute("DELETE FROM runtime_events WHERE sequence <= 3")
        connection.commit()

    frames = _run_stream(store, broker, after_event_id=seeded[1].event_id)
    assert [_frame_event(frame) for frame in frames] == ["stream.gap"]
    assert _frame_id(frames[0]) == seeded[1].event_id


def test_workspace_stream_ignores_unmapped_event_types(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    _append(
        store,
        event_id="evt-internal",
        event_type=RuntimeEventType.AGENT_TEXT_DELTA,
        target_id="tgt-a",
        text="[REDACTED] model stream",
    )
    _append(
        store,
        event_id="evt-investigation",
        event_type=RuntimeEventType.INVESTIGATION_CREATED,
        target_id="tgt-a",
        investigation_id="inv-mapped",
    )

    changed = _changed_frames(_run_stream(store, broker))
    assert [frame["event_id"] for frame in changed] == ["evt-investigation"]


def test_workspace_stream_maps_investigation_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    _append(
        store,
        event_id="evt-inv",
        event_type=RuntimeEventType.INVESTIGATION_STARTED,
        target_id="tgt-a",
        investigation_id="inv-9",
    )

    changed = _changed_frames(_run_stream(store, broker))
    assert changed[0]["resource_kind"] == "investigation"
    assert changed[0]["resource_id"] == "inv-9"
    assert changed[0]["target_id"] == "tgt-a"
    assert changed[0]["service_id"] is None
    assert changed[0]["occurred_at"]


def test_workspace_stream_maps_service_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    _append(
        store,
        event_id="evt-log",
        event_type=RuntimeEventType.LOG_SUBSCRIPTION_STARTED,
        target_id="tgt-a",
        service_name="payment-api",
    )

    changed = _changed_frames(_run_stream(store, broker))
    assert changed[0]["resource_kind"] == "service"
    assert changed[0]["resource_id"] == "payment-api"
    assert changed[0]["service_id"] == "payment-api"
    assert changed[0]["target_id"] == "tgt-a"


def test_workspace_stream_maps_overview_and_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    _append(
        store,
        event_id="evt-project",
        event_type=RuntimeEventType.PROJECT_CREATED,
    )
    _append(
        store,
        event_id="evt-evidence",
        event_type=RuntimeEventType.EVIDENCE_APPENDED,
        investigation_id="inv-5",
    )

    changed = _changed_frames(_run_stream(store, broker))
    by_id = {frame["event_id"]: frame for frame in changed}
    assert by_id["evt-project"]["resource_kind"] == "overview"
    assert by_id["evt-project"]["resource_id"] is None
    assert by_id["evt-evidence"]["resource_kind"] == "evidence"
    assert by_id["evt-evidence"]["resource_id"] == "inv-5"


def test_workspace_stream_does_not_forward_sensitive_payload(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    _append(
        store,
        event_id="evt-secret",
        event_type=RuntimeEventType.INVESTIGATION_CREATED,
        investigation_id="inv-x",
        target_id="tgt-a",
        api_key="super-secret",
        password="hunter2",
        backup_plaintext="TOP SECRET",
    )

    frames = _run_stream(store, broker)
    rendered = "\n".join(frames)
    assert "super-secret" not in rendered
    assert "hunter2" not in rendered
    assert "TOP SECRET" not in rendered
    data = _changed_frames(frames)[0]
    assert set(data) == {
        "schema_version",
        "event_id",
        "event_type",
        "occurred_at",
        "resource_kind",
        "resource_id",
        "target_id",
        "service_id",
    }


def test_workspace_stream_heartbeat_on_idle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    _seed(store, 2)

    frames = _run_stream(
        store, broker, heartbeat_seconds=0.05, settle_seconds=0.2
    )
    assert any(_is_heartbeat(frame) for frame in frames)
    assert any("heartbeat" in frame for frame in frames)


def test_workspace_stream_disconnect_cleans_subscription(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    _append(
        store,
        event_id="evt-1",
        event_type=RuntimeEventType.INVESTIGATION_CREATED,
        target_id="tgt-a",
        investigation_id="inv-1",
    )

    async def scenario() -> None:
        stream = WorkspaceEventStream(events=store, broker=broker)
        generator = stream.run(
            after_event_id=None, target_id=None, allowed_target_ids=None
        )
        first = await generator.__anext__()
        assert _frame_data(first)["event_id"] == "evt-1"
        assert broker._subscribers  # active while the stream is connected
        await generator.aclose()
        assert broker._subscribers == []

    asyncio.run(scenario())


def test_workspace_stream_delivers_live_invalidation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    frames: list[str] = []

    async def scenario() -> None:
        stream = WorkspaceEventStream(
            events=store, broker=broker, heartbeat_seconds=1.0
        )
        generator = stream.run(
            after_event_id=None, target_id=None, allowed_target_ids=None
        )
        task = asyncio.create_task(_consume(frames, generator))
        await asyncio.sleep(0.05)
        stored = _append(
            store,
            event_id="evt-live",
            event_type=RuntimeEventType.CONCLUSION_CREATED,
            target_id="tgt-a",
            investigation_id="inv-live",
        )
        await broker.publish(stored)
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    asyncio.run(scenario())
    live = [frame for frame in frames if _frame_id(frame) == "evt-live"]
    assert len(live) == 1
    assert _frame_event(live[0]) == "resource.changed"
    assert _frame_data(live[0])["resource_id"] == "inv-live"
