"""Recoverable CLI event stream: durable replay into a race-free live feed.

The CLI connects over ``/ws/v1/cli-events`` and asks for every durable event
after an applied sequence (its local cursor).  This module owns the
replay-to-live handoff:

1. subscribe to the in-memory broker *before* capturing the durable high-water
   sequence, so no event published between the two steps is lost;
2. replay 500-row durable pages up to that high water;
3. switch to live, deduplicating only by event sequence (replay already reached
   the high water, so the only overlap is an event re-delivered on the boundary,
   which is a single bounded value, never an unbounded dedupe set).

Backpressure is explicit: the broker records how many frames it had to evict for
a subscriber that cannot keep up.  When that overflow becomes non-zero the
stream emits ``stream.slow_consumer`` (when the outbound loop is still able to
write) and closes ``1013`` instead of silently dropping history.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType

#: Outbound queue bound for a CLI subscriber (max unacked frames buffered).
CLI_STREAM_QUEUE_SIZE = 256

#: Durable page size used while replaying history.
_REPLAY_PAGE_SIZE = 500

#: Milliseconds of idle time before a ``stream.heartbeat`` is emitted.
HEARTBEAT_INTERVAL_SECONDS = 15.0


@dataclass(frozen=True)
class EventFilter:
    session_id: str | None = None
    target_id: str | None = None
    investigation_id: str | None = None
    event_types: tuple[str, ...] = ()


async def _send(send: Callable[[str], Awaitable[None]], frame: dict[str, object]) -> None:
    await send(_encode(frame))


def _encode(frame: dict[str, object]) -> str:
    import json

    return json.dumps(frame, separators=(",", ":"))


class CliEventStream:
    """Drives one CLI WebSocket: hello, replay, then live delivery."""

    def __init__(
        self,
        *,
        events: RuntimeEventStore,
        broker: RuntimeEventBroker,
        filter: EventFilter,
        allowed_target_ids: frozenset[str] | None,
        resolve_investigation_id: Callable[[str], str | None] | None = None,
        heartbeat_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._events = events
        self._broker = broker
        self._filter = filter
        self._allowed_target_ids = allowed_target_ids
        self._resolve_investigation_id = resolve_investigation_id
        self._heartbeat_seconds = heartbeat_seconds

    async def run(
        self,
        *,
        after_sequence: int,
        send: Callable[[str], Awaitable[None]],
        close: Callable[[int, str], Awaitable[None]],
    ) -> None:
        await _send(send, _hello())
        # Subscribe before capturing the high-water so no live event is missed
        # between the durable scan and the live transition.
        async with self._broker.subscribe() as live_queue:
            high_water = self._high_water_sequence()
            await self._replay(send, close, after_sequence, high_water)
            await self._live(send, close, live_queue, from_sequence=high_water)

    def _high_water_sequence(self) -> int:
        page = self._events.list_page(
            after_sequence=0,
            limit=1,
            **self._filter_params(),
        )
        return page.latest_sequence

    def _filter_params(self) -> dict[str, object]:
        f = self._filter
        params: dict[str, object] = {
            "session_id": f.session_id,
            "target_id": f.target_id,
            "investigation_id": f.investigation_id,
        }
        if f.event_types:
            params["event_types"] = tuple(
                RuntimeEventType(t) for t in f.event_types
            )
        if self._allowed_target_ids is not None:
            params["allowed_target_ids"] = self._allowed_target_ids
        return params

    async def _replay(
        self,
        send: Callable[[str], Awaitable[None]],
        close: Callable[[int, str], Awaitable[None]],
        after_sequence: int,
        high_water: int,
    ) -> None:
        """Deliver durable events strictly after *after_sequence* up to *high_water*."""
        first_page = self._events.list_page(
            after_sequence=after_sequence, limit=1, **self._filter_params()
        )
        if after_sequence > 0 and first_page.earliest_available_sequence > after_sequence + 1:
            # History before the client's cursor was pruned; never resume at the
            # client's cursor (that would silently skip events) nor at the latest.
            await _send(
                send,
                {
                    "schema_version": 1,
                    "event_type": "stream.gap",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "payload": {
                        "requested_after_sequence": after_sequence,
                        "earliest_available_sequence": first_page.earliest_available_sequence,
                    },
                },
            )
            await close(1012, "event history no longer available")
            return
        cursor = after_sequence
        while True:
            page = self._events.list_page(
                after_sequence=cursor,
                limit=_REPLAY_PAGE_SIZE,
                **self._filter_params(),
            )
            for event in page.items:
                if event.sequence > high_water:
                    return
                await _send(send, _envelope(event))
            if not page.has_more or not page.items:
                return
            cursor = page.next_after_sequence

    async def _live(
        self,
        send: Callable[[str], Awaitable[None]],
        close: Callable[[int, str], Awaitable[None]],
        live_queue: asyncio.Queue[RuntimeEvent],
        *,
        from_sequence: int,
    ) -> None:
        last_activity = datetime.now(UTC)
        while True:
            try:
                event = await asyncio.wait_for(
                    live_queue.get(), timeout=self._heartbeat_seconds
                )
            except asyncio.TimeoutError:
                if self._broker.dropped_count(live_queue):
                    await self._close_slow(send, close)
                    return
                idle_for = (datetime.now(UTC) - last_activity).total_seconds()
                if idle_for >= self._heartbeat_seconds:
                    await _send(send, _heartbeat())
                continue
            if self._broker.dropped_count(live_queue):
                await self._close_slow(send, close)
                return
            last_activity = datetime.now(UTC)
            if event.sequence <= from_sequence:
                # Already delivered during replay; ignore the boundary overlap.
                continue
            # The broker is shared by every session.  Durable replay applies
            # the SQL filter, but live delivery comes from the fan-out queue
            # and must apply the same predicate again; otherwise a CLI for one
            # session renders another session's tool calls and completion
            # markers (the source of misleading stale `running` rows).
            if not self._matches_filter(event):
                continue
            await _send(send, _envelope(event))

    def _refresh_investigation_filter(self) -> None:
        if (
            self._filter.session_id is None
            or self._filter.investigation_id is not None
            or self._resolve_investigation_id is None
        ):
            return
        try:
            investigation_id = self._resolve_investigation_id(self._filter.session_id)
        except Exception:  # noqa: BLE001 - binding may not be durable yet
            return
        if investigation_id is not None:
            self._filter = EventFilter(
                session_id=self._filter.session_id,
                target_id=self._filter.target_id,
                investigation_id=investigation_id,
                event_types=self._filter.event_types,
            )

    def _matches_filter(self, event: RuntimeEvent) -> bool:
        self._refresh_investigation_filter()
        payload = event.payload
        f = self._filter
        # Product messages/operation events carry session_id, while the
        # investigation runtime predates product sessions and carries only
        # investigation_id. A session stream must accept either correlation
        # path; requiring both drops every tool event once the session has
        # been bound to its investigation.
        event_session_id = payload.get("session_id")
        event_investigation_id = payload.get("investigation_id")
        if f.session_id is not None or f.investigation_id is not None:
            matches_session = (
                f.session_id is not None and event_session_id == f.session_id
            )
            matches_investigation = (
                f.investigation_id is not None
                and event_investigation_id == f.investigation_id
            )
            if not matches_session and not matches_investigation:
                return False
        if f.target_id is not None and payload.get("target_id") != f.target_id:
            return False
        if f.event_types and event.event_type not in tuple(
            RuntimeEventType(t) for t in f.event_types
        ):
            return False
        if self._allowed_target_ids is not None:
            target_id = payload.get("target_id")
            if target_id is not None and target_id not in self._allowed_target_ids:
                return False
        return True

    async def _close_slow(
        self,
        send: Callable[[str], Awaitable[None]],
        close: Callable[[int, str], Awaitable[None]],
    ) -> None:
        try:
            await _send(
                send,
                {
                    "schema_version": 1,
                    "event_type": "stream.slow_consumer",
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
            )
        except Exception:  # noqa: BLE001 - the code below must still close
            pass
        await close(1013, "subscriber too slow")


def _hello() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_type": "stream.hello",
        "occurred_at": datetime.now(UTC).isoformat(),
    }


def _heartbeat() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_type": "stream.heartbeat",
        "occurred_at": datetime.now(UTC).isoformat(),
    }


def _envelope(event: RuntimeEvent) -> dict[str, object]:
    payload = event.payload
    return {
        "schema_version": 1,
        "event_id": event.event_id,
        "sequence": event.sequence,
        "event_type": event.event_type.value,
        "session_id": _string(payload.get("session_id")),
        "target_id": _string(payload.get("target_id")),
        "investigation_id": _string(payload.get("investigation_id")),
        "occurred_at": event.occurred_at.isoformat(),
        "payload": payload,
    }


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = [
    "CLI_STREAM_QUEUE_SIZE",
    "CliEventStream",
    "EventFilter",
    "HEARTBEAT_INTERVAL_SECONDS",
]
