"""Unit tests for the recoverable CLI event stream handoff.

These drive :class:`CliEventStream` directly against a real SQLite event store
and in-memory broker, capturing outbound frames through a fake ``send`` so the
replay-to-live handoff, gap detection and overflow behavior are asserted without
a full HTTP stack.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType
from incidentlens_control_plane.streams.cli import CliEventStream, EventFilter


def _store(tmp_path: Path) -> RuntimeEventStore:
    store = RuntimeEventStore(lambda: sqlite3.connect(tmp_path / "events.db"))
    store.migrate()
    return store


def _seed(store: RuntimeEventStore, count: int, target_id: str) -> int:
    last = 0
    for index in range(1, count + 1):
        stored = store.append(
            RuntimeEvent(
                event_id=f"evt-ws-{target_id}-{index}",
                event_type=RuntimeEventType.PROJECT_CREATED,
                occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
                payload={"target_id": target_id, "session_id": f"ses-{index % 3}"},
            )
        )
        last = stored.sequence
    return last


def _run_stream(
    store: RuntimeEventStore,
    broker: RuntimeEventBroker,
    *,
    after_sequence: int = 0,
    filter: EventFilter = EventFilter(),
    settle_seconds: float = 0.2,
):
    frames: list[dict[str, object]] = []

    async def send(text: str) -> None:
        frames.append(json.loads(text))

    async def close(code: int, reason: str) -> None:
        pass

    async def scenario() -> None:
        stream = CliEventStream(
            events=store,
            broker=broker,
            filter=filter,
            allowed_target_ids=None,
        )
        task = asyncio.create_task(
            stream.run(after_sequence=after_sequence, send=send, close=close)
        )
        # Let replay finish (it runs synchronously fast) then the live loop idle.
        await asyncio.sleep(settle_seconds)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    asyncio.run(scenario())
    return frames


def _event_sequences(frames: list[dict[str, object]]) -> list[int]:
    return [
        frame["sequence"]
        for frame in frames
        if "sequence" in frame and frame.get("event_type") != "stream.hello"
    ]


def test_cli_stream_replays_1501_events_without_gap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=16)
    _seed(store, 1501, "tgt-a")

    frames = _run_stream(store, broker)
    sequences = _event_sequences(frames)
    assert len(sequences) == 1501
    assert sequences == list(range(1, 1502))


def test_cli_stream_resumes_from_after_sequence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    _seed(store, 300, "tgt-a")

    frames = _run_stream(store, broker, after_sequence=200)
    assert _event_sequences(frames) == list(range(201, 301))


def test_cli_stream_filters_by_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    _seed(store, 50, "tgt-a")
    _seed(store, 50, "tgt-b")

    frames = _run_stream(
        store, broker, filter=EventFilter(target_id="tgt-a")
    )
    sequences = _event_sequences(frames)
    assert len(sequences) == 50
    assert sequences == list(range(1, 51))


def test_cli_stream_live_filter_unions_session_and_investigation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=8)
    frames: list[dict[str, object]] = []

    async def send(text: str) -> None:
        frames.append(json.loads(text))

    async def close(code: int, reason: str) -> None:
        pass

    async def scenario() -> None:
        stream = CliEventStream(
            events=store,
            broker=broker,
            filter=EventFilter(session_id="session-1"),
            allowed_target_ids=None,
            resolve_investigation_id=lambda _: "investigation-1",
        )
        task = asyncio.create_task(
            stream.run(after_sequence=0, send=send, close=close)
        )
        await asyncio.sleep(0.02)
        for event_id, payload in (
            ("evt-session", {"session_id": "session-1"}),
            ("evt-investigation", {"investigation_id": "investigation-1"}),
            ("evt-other", {"investigation_id": "investigation-2"}),
        ):
            stored = store.append(
                RuntimeEvent(
                    event_id=event_id,
                    event_type=RuntimeEventType.TOOL_PROPOSED,
                    occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
                    payload=payload,
                )
            )
            await broker.publish(stored)
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert [frame["event_id"] for frame in frames if "event_id" in frame] == [
        "evt-session",
        "evt-investigation",
    ]


def test_cli_stream_sends_hello_first_and_heartbeat_on_idle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broker = RuntimeEventBroker(queue_size=4)
    _seed(store, 5, "tgt-a")

    frames = _run_stream(store, broker, settle_seconds=0.05)
    assert frames[0]["event_type"] == "stream.hello"
