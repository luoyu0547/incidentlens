"""Persistent opt-in log subscription manager.

Each running subscription owns one reader task, one writer task, and one
bounded ``asyncio.Queue``.  The reader polls the source and enqueues raw
lines; the writer drains the queue in batches, runs the redaction pipeline,
persists the records, and only then advances the stored cursor.  Reader
errors are transient by design: a failed poll is logged and retried on the
next interval so a running subscription survives a temporary transport or
file error (full backoff/retry arrives with the docker stream task).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType
from incidentlens_control_plane.logs.service import LogService
from incidentlens_control_plane.logs.sources import FileLogSource
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.logs.types import (
    LogScope,
    LogSourceKind,
    LogSubscription,
    RawLogLine,
)
from incidentlens_control_plane.project_registry.types import TargetRegistration

logger = logging.getLogger(__name__)


class TooManyActiveSubscriptions(Exception):
    """Raised when creating a subscription would exceed ``max_active``."""


@dataclass(slots=True)
class _QueuedLine:
    """A raw log line paired with the generation of the poll that produced it."""

    raw: RawLogLine
    generation: str


@dataclass(slots=True)
class _SubscriptionTasks:
    reader: asyncio.Task[None]
    writer: asyncio.Task[None]
    queue: asyncio.Queue[_QueuedLine]


class LogSubscriptionManager:
    """Owns the persistent opt-in log subscription state machine.

    ``create``/``resume`` move a subscription to the running set; ``pause``/
    ``delete``/``close_all`` cancel its tasks.  ``max_active`` is mutable so
    callers can tune the active cap at runtime.
    """

    def __init__(
        self,
        *,
        store: LogStore,
        service: LogService,
        events: RuntimeEventStore,
        broker: RuntimeEventBroker,
        settings: RuntimeSettings,
    ) -> None:
        self._store = store
        self._service = service
        self._events = events
        self._broker = broker
        self._settings = settings
        self.max_active: int = settings.max_active_log_subscriptions
        self._queue_size: int = settings.log_subscription_queue_size
        self._batch_size: int = settings.log_subscription_batch_size
        self._poll_interval: float = settings.log_file_poll_interval_seconds
        self._running: dict[str, _SubscriptionTasks] = {}

    def running_subscription_ids(self) -> set[str]:
        """Return the ids of subscriptions with live reader/writer tasks."""
        return set(self._running)

    # --- state transitions ---

    async def create(
        self,
        *,
        project_id: str,
        target_id: str,
        service_name: str,
        source_kind: LogSourceKind,
        scope: LogScope,
        source_ref: str,
        opt_in_streaming: bool,
        created_by: str,
        now: datetime | None = None,
    ) -> LogSubscription:
        """Persist a subscription and start streaming it.

        Streaming requires an explicit opt-in; ``opt_in_streaming=False`` is
        rejected with a ``ValueError`` before anything is persisted.  When the
        running set is already at ``max_active`` a ``TooManyActiveSubscriptions``
        is raised and nothing is persisted.
        """
        if not opt_in_streaming:
            raise ValueError("opt_in_streaming=true is required for streaming")
        if len(self.running_subscription_ids()) >= self.max_active:
            raise TooManyActiveSubscriptions(
                f"active log subscriptions exceed max_active={self.max_active}"
            )
        now = now or datetime.now(UTC)
        subscription = self._store.create_subscription(
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            source_kind=source_kind,
            scope=scope,
            source_ref=source_ref,
            opt_in_streaming=True,
            created_by=created_by,
            now=now,
        )
        await self._start(subscription.subscription_id)
        return subscription

    async def start_active_opt_in(self) -> None:
        """Restore every active opt-in subscription after a restart.

        Subscriptions are started in store order and the same ``max_active``
        cap applies, so a paused subscription is never restarted (its status is
        not active) and an over-cap set is only partially restored.
        """
        for subscription in self._store.list_active_opt_in_subscriptions():
            if len(self.running_subscription_ids()) >= self.max_active:
                return
            await self._start(subscription.subscription_id)

    async def pause(
        self, subscription_id: str, *, now: datetime | None = None
    ) -> LogSubscription:
        """Stop the reader/writer tasks and mark the subscription paused.

        The stored cursor is intentionally preserved so ``resume`` continues
        from the same offset.
        """
        now = now or datetime.now(UTC)
        await self._stop(subscription_id)
        subscription = self._store.pause_subscription(subscription_id, now=now)
        await self._emit(RuntimeEventType.LOG_SUBSCRIPTION_PAUSED, subscription)
        return subscription

    async def resume(
        self, subscription_id: str, *, now: datetime | None = None
    ) -> LogSubscription:
        """Mark the subscription active and restart its reader/writer tasks."""
        now = now or datetime.now(UTC)
        subscription = self._store.resume_subscription(subscription_id, now=now)
        await self._start(subscription.subscription_id)
        return subscription

    async def delete(
        self, subscription_id: str, *, now: datetime | None = None
    ) -> LogSubscription:
        """Stop the reader/writer tasks and mark the subscription deleted."""
        now = now or datetime.now(UTC)
        await self._stop(subscription_id)
        return self._store.delete_subscription(subscription_id, now=now)

    async def close_all(self) -> None:
        """Cancel and await every running reader/writer task.

        SSH sessions are owned by the session manager and are intentionally
        left open here; the lifespan shuts them down separately.
        """
        for subscription_id in list(self._running):
            await self._stop(subscription_id)

    # --- task management ---

    async def _start(self, subscription_id: str) -> None:
        subscription = self._store.get_subscription(subscription_id)
        if subscription is None:
            raise KeyError(f"subscription not found: {subscription_id}")
        if subscription_id in self._running:
            return
        queue: asyncio.Queue[_QueuedLine] = asyncio.Queue(maxsize=self._queue_size)
        reader = asyncio.create_task(self._reader_loop(subscription, queue))
        writer = asyncio.create_task(self._writer_loop(subscription, queue))
        self._running[subscription_id] = _SubscriptionTasks(
            reader=reader, writer=writer, queue=queue
        )
        await self._emit(RuntimeEventType.LOG_SUBSCRIPTION_STARTED, subscription)

    async def _stop(self, subscription_id: str) -> None:
        tasks = self._running.pop(subscription_id, None)
        if tasks is None:
            return
        tasks.reader.cancel()
        tasks.writer.cancel()
        await asyncio.gather(tasks.reader, tasks.writer, return_exceptions=True)

    # --- reader ---

    async def _reader_loop(
        self, subscription: LogSubscription, queue: asyncio.Queue[_QueuedLine]
    ) -> None:
        try:
            while True:
                try:
                    if subscription.source_kind == LogSourceKind.FILE:
                        await self._poll_file(subscription, queue)
                    else:
                        # Docker streaming arrives in a later task; log and poll on.
                        logger.warning(
                            "unsupported log source for subscription %s: %s",
                            subscription.subscription_id,
                            subscription.source_kind.value,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "log subscription reader error for %s; retrying",
                        subscription.subscription_id,
                    )
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            pass

    async def _poll_file(
        self, subscription: LogSubscription, queue: asyncio.Queue[_QueuedLine]
    ) -> None:
        source = FileLogSource(self._service._sessions)
        target = self._resolve_target(subscription)
        cursor = self._store.get_cursor(subscription.subscription_id)
        path = PurePosixPath(subscription.source_ref)
        result = await source.stream(subscription, target, path, cursor)
        if result.rotated:
            logger.warning(
                "log source rotated for subscription %s; restarting at offset 0",
                subscription.subscription_id,
            )
        for raw in result.lines:
            await queue.put(_QueuedLine(raw=raw, generation=result.generation))

    def _resolve_target(self, subscription: LogSubscription) -> TargetRegistration:
        project = self._service._projects.get(subscription.project_id)
        return self._service._resolve_target(project, subscription.target_id)

    # --- writer ---

    async def _writer_loop(
        self, subscription: LogSubscription, queue: asyncio.Queue[_QueuedLine]
    ) -> None:
        try:
            while True:
                batch: list[_QueuedLine] = []
                try:
                    first = await queue.get()
                except asyncio.CancelledError:
                    raise
                batch.append(first)
                while len(batch) < self._batch_size:
                    try:
                        batch.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                try:
                    await self._write_batch(subscription, batch)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A transient persistence error leaves the cursor unadvanced,
                    # so the reader re-reads the same lines on the next poll.
                    logger.exception(
                        "log subscription writer error for %s; retrying",
                        subscription.subscription_id,
                    )
        except asyncio.CancelledError:
            pass

    async def _write_batch(
        self, subscription: LogSubscription, batch: list[_QueuedLine]
    ) -> None:
        """Process, persist, and only then advance the stored cursor."""
        now = datetime.now(UTC)
        raw_lines = tuple(item.raw for item in batch)
        records = self._service.process_raw_lines(
            raw_lines, now=now, subscription=subscription
        )
        self._store.append_batch(records)
        latest = batch[-1]
        self._store.upsert_cursor(
            subscription_id=subscription.subscription_id,
            cursor=latest.raw.cursor,
            generation=latest.generation,
            observed_at=latest.raw.observed_at,
            now=now,
        )

    # --- runtime events ---

    async def _emit(self, event_type: RuntimeEventType, subscription: LogSubscription) -> None:
        """Persist and publish a safe lifecycle event for a subscription."""
        try:
            event = RuntimeEvent(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                sequence=0,
                event_type=event_type,
                occurred_at=datetime.now(UTC),
                payload={
                    "subscription_id": subscription.subscription_id,
                    "project_id": subscription.project_id,
                    "target_id": subscription.target_id,
                    "service_name": subscription.service_name,
                    "source_kind": subscription.source_kind.value,
                    "scope": subscription.scope.value,
                    "source_ref": subscription.source_ref,
                    "status": subscription.status.value,
                },
            )
            stored = self._events.append(event)
            await self._broker.publish(stored)
        except Exception:
            logger.exception("failed to emit runtime event")
