"""Persistent opt-in log subscription manager.

Each running subscription owns one reader task, one writer task, and one
bounded ``asyncio.Queue``.  The reader polls/streams the source and enqueues
raw lines; the writer drains the queue in batches, runs the redaction
pipeline, persists the records, and only then advances the stored cursor.
Reader failures back off exponentially (capped at 60s) and move the
subscription to status ``error`` after ``max_failures``; docker streams add a
backpressure reconnect loop that closes the process, emits a safe
``log.backpressure`` event, and resumes from the last committed cursor.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import (
    JsonValue,
    RuntimeEvent,
    RuntimeEventType,
)
from incidentlens_control_plane.logs.redaction import redact_message
from incidentlens_control_plane.logs.service import LogService
from incidentlens_control_plane.logs.sources import DockerLogSource, FileLogSource
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.logs.types import (
    LogRecord,
    LogScope,
    LogSourceKind,
    LogSubscription,
    LogSubscriptionStatus,
    RawLogLine,
)
from incidentlens_control_plane.project_registry.types import TargetRegistration

logger = logging.getLogger(__name__)

# Hostname-like tokens (``dev-a.example.test``) are redacted from persisted
# error summaries so a target host can never leak into a subscription record.
_HOST_RE = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b")


def _summarize_error(error: BaseException) -> str:
    """Return a redacted, persistable summary of a reader error.

    The message passes through the message redactor (secrets, tokens, emails,
    IPs) and hostname-like tokens are additionally redacted so neither a
    credential nor a target host survives into ``last_error_redacted``.
    """
    redacted = redact_message(str(error)).message_redacted
    return _HOST_RE.sub("[REDACTED_HOST]", redacted)


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
        # Reader retry/backoff policy.  ``max_failures`` and the backoff cap are
        # mutable so callers can tune them at runtime (and tests can shrink
        # them); the put timeout guards the bounded queue against a slow writer.
        self.max_failures: int = 3
        self._max_backoff_seconds: float = 60.0
        self._queue_put_timeout: float = 5.0
        self._failures: dict[str, int] = {}
        self._queue_size: int = settings.log_subscription_queue_size
        self._batch_size: int = settings.log_subscription_batch_size
        self._poll_interval: float = settings.log_file_poll_interval_seconds
        self._running: dict[str, _SubscriptionTasks] = {}
        # Live record fan-out for WebSocket subscribers, keyed by
        # subscription_id.  A subscription need not be running (reader/writer
        # tasks) to have live subscribers; the WebSocket handler registers here
        # before replaying durable records so no live record is missed.
        self._live_queues: dict[str, list[asyncio.Queue[LogRecord]]] = {}

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
        await self._emit_safe_event(
            RuntimeEventType.LOG_SUBSCRIPTION_PAUSED, subscription
        )
        return subscription

    async def resume(
        self, subscription_id: str, *, now: datetime | None = None
    ) -> LogSubscription:
        """Mark the subscription active and restart its reader/writer tasks."""
        now = now or datetime.now(UTC)
        subscription = self._store.resume_subscription(subscription_id, now=now)
        await self._start(
            subscription.subscription_id,
            event_type=RuntimeEventType.LOG_SUBSCRIPTION_RESUMED,
        )
        return subscription

    async def delete(
        self, subscription_id: str, *, now: datetime | None = None
    ) -> LogSubscription:
        """Stop the reader/writer tasks and mark the subscription deleted."""
        now = now or datetime.now(UTC)
        await self._stop(subscription_id)
        subscription = self._store.delete_subscription(subscription_id, now=now)
        await self._emit_safe_event(
            RuntimeEventType.LOG_SUBSCRIPTION_DELETED, subscription
        )
        return subscription

    async def close_all(self) -> None:
        """Cancel and await every running reader/writer task.

        SSH sessions are owned by the session manager and are intentionally
        left open here; the lifespan shuts them down separately.
        """
        for subscription_id in list(self._running):
            await self._stop(subscription_id)

    # --- task management ---

    async def _start(
        self,
        subscription_id: str,
        *,
        event_type: RuntimeEventType = RuntimeEventType.LOG_SUBSCRIPTION_STARTED,
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        if subscription is None:
            raise KeyError(f"subscription not found: {subscription_id}")
        if subscription_id in self._running:
            return
        # A restart (create/resume/recovery) resets retry state so the failure
        # counter and backoff do not carry over from a prior errored run.
        self._failures.pop(subscription_id, None)
        queue: asyncio.Queue[_QueuedLine] = asyncio.Queue(maxsize=self._queue_size)
        reader = asyncio.create_task(self._reader_loop(subscription, queue))
        writer = asyncio.create_task(self._writer_loop(subscription, queue))
        self._running[subscription_id] = _SubscriptionTasks(
            reader=reader, writer=writer, queue=queue
        )
        await self._emit_safe_event(event_type, subscription)

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
        """Poll/stream the source and enqueue raw lines, retrying with backoff.

        File subscriptions poll on an interval; docker subscriptions stream a
        long-lived ``--follow`` process with an internal backpressure reconnect
        loop.  Reader failures back off exponentially (capped at 60s) and the
        subscription is moved to status ``error`` after ``max_failures``
        consecutive failures.
        """
        backoff = 1.0
        try:
            while True:
                try:
                    if subscription.source_kind == LogSourceKind.FILE:
                        await self._poll_file(subscription, queue)
                    else:
                        await self._stream_docker(subscription, queue)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "log subscription reader error for %s; retrying",
                        subscription.subscription_id,
                    )
                    reached = await self._record_failure(
                        subscription.subscription_id, exc
                    )
                    if reached:
                        return
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self._max_backoff_seconds)
                    continue
                self._failures.pop(subscription.subscription_id, None)
                backoff = 1.0
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
            await self._emit_safe_event(
                RuntimeEventType.LOG_SOURCE_ROTATED, subscription
            )
        for raw in result.lines:
            await queue.put(_QueuedLine(raw=raw, generation=result.generation))

    async def _stream_docker(
        self, subscription: LogSubscription, queue: asyncio.Queue[_QueuedLine]
    ) -> None:
        """Stream docker container logs, reconnecting on backpressure.

        Lines are enqueued exactly like file polls.  When the bounded queue
        stays full past the put timeout the process is closed (via the source
        generator's ``finally``), a ``log.backpressure`` event is emitted, and
        the stream reconnects from the last committed cursor.
        """
        target = self._resolve_target(subscription)
        session = await self._service._sessions.connect(target)
        source = DockerLogSource(lambda _target: session.transport)
        while True:
            cursor = self._store.get_cursor(subscription.subscription_id)
            try:
                async for line in source.stream(subscription, target, cursor):
                    item = _QueuedLine(raw=line, generation=line.cursor)
                    try:
                        await asyncio.wait_for(
                            queue.put(item), timeout=self._queue_put_timeout
                        )
                    except asyncio.TimeoutError:
                        await self._emit_safe_event(
                            RuntimeEventType.LOG_BACKPRESSURE,
                            subscription,
                            reason="queue full",
                        )
                        break
                else:
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                raise

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
        await self._emit_safe_event(
            RuntimeEventType.LOG_BATCH_WRITTEN,
            subscription,
            lines=len(batch),
            records_written=len(records),
        )

    # --- runtime events ---

    def _safe_payload(
        self, subscription: LogSubscription, **extra: JsonValue
    ) -> dict[str, JsonValue]:
        """Build an event payload with ONLY safe subscription metadata.

        Never includes raw log text, target hosts, or credentials: only the
        subscription identity, project/target ids, service name, source kind,
        scope, source ref, status, and caller-supplied safe counts/summaries.
        """
        payload: dict[str, JsonValue] = {
            "subscription_id": subscription.subscription_id,
            "project_id": subscription.project_id,
            "target_id": subscription.target_id,
            "service_name": subscription.service_name,
            "source_kind": subscription.source_kind.value,
            "scope": subscription.scope.value,
            "source_ref": subscription.source_ref,
            "status": subscription.status.value,
        }
        payload.update(extra)
        return payload

    async def _emit_safe_event(
        self,
        event_type: RuntimeEventType,
        subscription: LogSubscription,
        **extra: JsonValue,
    ) -> None:
        """Persist and publish a safe lifecycle event for a subscription.

        All log.* events flow through here so the redaction discipline is
        enforced in one place.
        """
        try:
            event = RuntimeEvent(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                sequence=0,
                event_type=event_type,
                occurred_at=datetime.now(UTC),
                payload=self._safe_payload(subscription, **extra),
            )
            stored = self._events.append(event)
            await self._broker.publish(stored)
        except Exception:
            logger.exception("failed to emit runtime event")

    # --- live record fan-out (WebSocket subscribers) ---

    @asynccontextmanager
    async def subscribe_records(
        self, subscription_id: str
    ) -> AsyncIterator[asyncio.Queue[LogRecord]]:
        """Register a queue for live records of a subscription.

        Broker-style fan-out: every registered queue for the subscription
        receives each live record.  Works for subscriptions that are not
        currently running a reader/writer task (the WebSocket handler registers
        before replaying durable records, so a record is never missed).  The
        queue is bounded and drops the oldest record when full.
        """
        queue: asyncio.Queue[LogRecord] = asyncio.Queue(maxsize=self._queue_size)
        self._live_queues.setdefault(subscription_id, []).append(queue)
        try:
            yield queue
        finally:
            queues = self._live_queues.get(subscription_id)
            if queues is not None:
                try:
                    queues.remove(queue)
                except ValueError:
                    pass
                if not queues:
                    self._live_queues.pop(subscription_id, None)

    def _publish_live(self, record: LogRecord) -> None:
        """Fan out a live record to every subscribed queue for its subscription.

        Never blocks: a full queue drops its oldest record, mirroring the
        runtime event broker's backpressure behavior.
        """
        subscription_id = record.subscription_id
        if subscription_id is None:
            return
        for queue in self._live_queues.get(subscription_id, ()):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(record)
            except asyncio.QueueFull:
                pass

    # --- test hooks and failure recording ---

    def publish_live_for_test(self, record: LogRecord) -> None:
        """Test hook: publish a live record to the subscription's live queues.

        No-op when no WebSocket is subscribed.  Uses the same ``_publish_live``
        fan-out the writer uses to notify live subscribers, so the WebSocket
        dedupe logic is exercised against the real publish path.
        """
        self._publish_live(record)

    async def force_backpressure_for_test(self, subscription_id: str) -> None:
        """Test hook: simulate a docker backpressure condition.

        Drives the real ``_emit_safe_event`` path so the emitted
        ``log.backpressure`` event is subject to the same redaction discipline
        as production events.
        """
        subscription = self._store.get_subscription(subscription_id)
        if subscription is None:
            return
        await self._emit_safe_event(
            RuntimeEventType.LOG_BACKPRESSURE, subscription, reason="queue full"
        )

    async def record_failure_for_test(
        self, subscription_id: str, error: Exception
    ) -> None:
        """Test hook: record a reader failure (see ``_record_failure``)."""
        await self._record_failure(subscription_id, error)

    async def _record_failure(
        self, subscription_id: str, error: BaseException
    ) -> bool:
        """Count a reader failure; at ``max_failures`` error the subscription.

        Returns True when the subscription was moved to status ``error`` (the
        reader loop should then stop retrying).  Absent or deleted subscriptions
        are a no-op: a reader failure racing a delete must never resurrect a
        deleted row, and a never-created id must not fabricate one.
        """
        existing = self._store.get_subscription(subscription_id)
        if existing is None or existing.status == LogSubscriptionStatus.DELETED:
            return False
        count = self._failures.get(subscription_id, 0) + 1
        self._failures[subscription_id] = count
        if count < self.max_failures:
            return False
        now = datetime.now(UTC)
        subscription = self._store.mark_subscription_error(
            subscription_id,
            last_error_redacted=_summarize_error(error),
            now=now,
        )
        if subscription is None:
            self._failures.pop(subscription_id, None)
            return False
        await self._emit_safe_event(
            RuntimeEventType.LOG_SUBSCRIPTION_ERROR,
            subscription,
            failure_count=count,
        )
        await self._teardown_running(subscription_id)
        return True

    async def _teardown_running(self, subscription_id: str) -> None:
        """Drop the running entry and stop its tasks after an error transition.

        Called when a subscription moves to status ``error`` so the entry does
        not stay wedged in ``_running`` (the writer would keep blocking on
        ``queue.get()`` and the id would keep counting toward ``max_active``),
        letting ``resume`` restart it cleanly.  The reader is cancelled too,
        unless it is the current caller (the reader loop observes the error and
        returns on its own).
        """
        tasks = self._running.pop(subscription_id, None)
        if tasks is None:
            return
        tasks.writer.cancel()
        await asyncio.gather(tasks.writer, return_exceptions=True)
        if tasks.reader is not asyncio.current_task():
            tasks.reader.cancel()
            await asyncio.gather(tasks.reader, return_exceptions=True)
