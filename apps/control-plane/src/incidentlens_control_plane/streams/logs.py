"""Cursor based durable log stream state machine."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from incidentlens_control_plane.logs.cursors import decode_log_cursor, encode_log_cursor
from incidentlens_control_plane.logs.types import LogRecord
from incidentlens_control_plane.logs.views import LogRecordView

PAGE_SIZE = 500
MAX_UNACKED = 500
HEARTBEAT_SECONDS = 15.0


def envelope(
    event_type: str,
    payload: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "schema_version": 1,
        "event_type": event_type,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    frame.update(extra)
    if payload is not None:
        frame["payload"] = payload
    return frame


def _record_payload(record: LogRecord) -> dict[str, Any]:
    return LogRecordView(
        log_id=record.log_id,
        cursor=record.cursor,
        occurred_at=record.observed_at,
        severity=record.severity,
        message=record.message_redacted,
        fields={},
    ).model_dump(mode="json")


class CursorLogStream:
    """Drive one product log subscriber; replay and live share one cursor."""

    def __init__(
        self,
        *,
        store: Any,
        subscriptions: Any,
        allowed_target_ids: frozenset[str] | None = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self.store = store
        self.subscriptions = subscriptions
        self.allowed_target_ids = allowed_target_ids
        self.heartbeat_seconds = heartbeat_seconds
        self.service_id: str | None = None
        self.target_id: str | None = None
        self.severity: str | None = None
        self.source_ref: str | None = None
        self.last_sequence = 0
        self.ack_sequence = 0
        self.paused = False

    def _records(
        self, *, after: int | None = None, snapshot: int | None = None
    ) -> tuple[tuple[LogRecord, ...], bool]:
        assert self.service_id is not None
        return self.store.list_product_page(
            service_name=self.service_id,
            after_sequence=after,
            snapshot_sequence=snapshot,
            limit=PAGE_SIZE,
            severity=self.severity,
            source_ref=self.source_ref,
            allowed_target_ids=self.allowed_target_ids,
        )

    async def backlog(self, send: Callable[..., Awaitable[Any]], cursor: str | None) -> int:
        try:
            after = decode_log_cursor(cursor) if cursor else 0
        except ValueError:
            await send(
                envelope(
                    "stream.gap",
                    {
                        "action": "refetch_http_snapshot",
                        "requested_cursor": cursor,
                        "earliest_cursor": encode_log_cursor(0),
                        "latest_cursor": encode_log_cursor(self.store.latest_product_sequence()),
                    },
                )
            )
            return -1
        snapshot = self.store.latest_product_sequence()
        current = after
        self.last_sequence = max(self.last_sequence, after)
        while True:
            records, more = self._records(after=current, snapshot=snapshot)
            for record in records:
                if record.stream_sequence <= self.last_sequence:
                    continue
                await send(
                    envelope(
                        "log.record",
                        _record_payload(record),
                        cursor=encode_log_cursor(record.stream_sequence),
                    )
                )
                self.last_sequence = record.stream_sequence
            if not records or not more:
                break
            current = records[-1].stream_sequence
        return snapshot

    async def run(
        self,
        *,
        send: Callable[..., Awaitable[Any]],
        receive: Callable[..., Awaitable[Any]],
        close: Callable[..., Awaitable[Any]],
        initial: dict[str, Any],
    ) -> None:
        async with self.subscriptions.subscribe_all_records() as queue:
            high_water = await self.backlog(send, initial.get("cursor"))
            if high_water < 0:
                await close(1012, "log history no longer available")
                return
            await send(
                envelope(
                    "log.subscribed",
                    {"service_id": self.service_id},
                    cursor=encode_log_cursor(self.last_sequence),
                )
            )
            while True:
                queue_task = asyncio.create_task(queue.get())
                control_task = asyncio.create_task(receive())
                done, pending = await asyncio.wait(
                    {queue_task, control_task},
                    timeout=self.heartbeat_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if not done:
                    await send(
                        envelope(
                            "stream.heartbeat",
                            {"cursor": encode_log_cursor(self.last_sequence)},
                        )
                    )
                    continue
                if control_task in done:
                    action = control_task.result()
                    if not isinstance(action, dict):
                        return
                    kind = action.get("action")
                    if kind == "ack":
                        self.ack(action.get("cursor", ""))
                    elif kind == "pause":
                        self.paused = True
                    elif kind == "resume":
                        self.paused = False
                        await self.backlog(send, action.get("cursor"))
                    elif kind == "update":
                        self.update(action)
                        await self.backlog(send, action.get("cursor"))
                    continue
                record = queue_task.result()
                if self.paused or record.stream_sequence <= self.last_sequence:
                    continue
                if self.service_id != record.service_name:
                    continue
                if self.target_id and self.target_id != record.target_id:
                    continue
                if self.severity and self.severity != record.severity.value:
                    continue
                if self.source_ref and self.source_ref != record.source_ref:
                    continue
                if self.last_sequence - self.ack_sequence >= MAX_UNACKED:
                    await send(
                        envelope(
                            "stream.slow_consumer",
                            {
                                "last_cursor": encode_log_cursor(self.last_sequence),
                                "action": "ack",
                            },
                        )
                    )
                    await close(1013, "subscriber too slow")
                    return
                await send(
                    envelope(
                        "log.record",
                        _record_payload(record),
                        cursor=encode_log_cursor(record.stream_sequence),
                    )
                )
                self.last_sequence = record.stream_sequence

    def update(self, action: dict[str, Any]) -> None:
        target = action.get("target_id")
        if (
            target is not None
            and self.allowed_target_ids is not None
            and target not in self.allowed_target_ids
        ):
            raise PermissionError("target not allowed")
        for key in ("service_id", "target_id", "severity", "source_ref"):
            if key in action:
                setattr(self, key, action[key])

    def ack(self, cursor: str) -> None:
        value = decode_log_cursor(cursor)
        self.ack_sequence = max(self.ack_sequence, min(value, self.last_sequence))


LogStream = CursorLogStream
__all__ = ["CursorLogStream", "LogStream", "PAGE_SIZE", "MAX_UNACKED"]
