"""LogSubscriptionManager state machine, recovery, and cursor tests."""

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest
from incidentlens_control_plane.logs.store import LogSearchFilters
from incidentlens_control_plane.logs.subscriptions import (
    LogSubscriptionManager,
    TooManyActiveSubscriptions,
    _QueuedLine,
)
from incidentlens_control_plane.logs.types import (
    InvalidSubscriptionTransition,
    LogScope,
    LogSourceKind,
    RawLogLine,
)

_APP_LOG = PurePosixPath("/var/log/payment/app.log")


async def _await_cursor(store, subscription_id: str, expected: str) -> None:
    """Wait until the stored cursor reaches ``expected`` (deterministic outcome)."""
    for _ in range(500):
        cursor = store.get_cursor(subscription_id)
        if cursor is not None and cursor.cursor == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"cursor for {subscription_id} never reached {expected!r}")


@pytest.mark.asyncio
async def test_create_subscription_requires_explicit_opt_in(
    manager: LogSubscriptionManager,
) -> None:
    with pytest.raises(ValueError, match="opt_in_streaming=true"):
        await manager.create(
            project_id="payments",
            target_id="dev-a",
            service_name="payment-api",
            source_kind=LogSourceKind.FILE,
            scope=LogScope.HOST,
            source_ref="/var/log/payment/app.log",
            opt_in_streaming=False,
            created_by="alice",
        )


@pytest.mark.asyncio
async def test_active_subscription_limit_returns_domain_error(
    manager: LogSubscriptionManager,
) -> None:
    manager.max_active = 1
    await manager.create(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
    )

    with pytest.raises(TooManyActiveSubscriptions):
        await manager.create(
            project_id="payments",
            target_id="dev-a",
            service_name="payment-api",
            source_kind=LogSourceKind.FILE,
            scope=LogScope.HOST,
            source_ref="/var/log/payment/other.log",
            opt_in_streaming=True,
            created_by="alice",
        )


@pytest.mark.asyncio
async def test_start_active_opt_in_restores_only_active_subscriptions(
    manager: LogSubscriptionManager, store
) -> None:
    active = store.create_subscription(
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
    paused = store.create_subscription(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/paused.log",
        opt_in_streaming=True,
        created_by="alice",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    store.pause_subscription(paused.subscription_id, now=datetime(2026, 8, 12, tzinfo=UTC))

    await manager.start_active_opt_in()

    assert active.subscription_id in manager.running_subscription_ids()
    assert paused.subscription_id not in manager.running_subscription_ids()


@pytest.mark.asyncio
async def test_pause_stops_task_and_preserves_cursor(
    manager: LogSubscriptionManager, store
) -> None:
    subscription = await manager.create(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
    )
    store.upsert_cursor(
        subscription_id=subscription.subscription_id,
        cursor="file:offset=42",
        generation="mtime=1:size=42",
        observed_at=datetime(2026, 8, 12, tzinfo=UTC),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    paused = await manager.pause(subscription.subscription_id)

    assert paused.status.value == "paused"
    assert store.get_cursor(subscription.subscription_id).cursor == "file:offset=42"
    assert subscription.subscription_id not in manager.running_subscription_ids()


@pytest.mark.asyncio
async def test_file_stream_persists_records_and_advances_cursor_after_commit(
    manager: LogSubscriptionManager, store, target_registration
) -> None:
    session = await manager._service._sessions.connect(target_registration)
    session.transport._files[_APP_LOG] = b"line one\nline two\n"
    manager._poll_interval = 0.01

    subscription = await manager.create(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
    )

    await _await_cursor(store, subscription.subscription_id, "file:offset=18")

    records = store.search(LogSearchFilters(project_id="payments"), limit=10)
    assert len(records) == 2
    assert {record.cursor for record in records} == {"file:offset=9", "file:offset=18"}
    assert all(record.source_ref == "/var/log/payment/app.log" for record in records)
    assert store.get_cursor(subscription.subscription_id).cursor == "file:offset=18"


@pytest.mark.asyncio
async def test_file_stream_rotation_resets_offset_to_zero(
    manager: LogSubscriptionManager, store, target_registration, caplog
) -> None:
    session = await manager._service._sessions.connect(target_registration)
    session.transport._files[_APP_LOG] = b"a\nb\nc\nd\n"
    manager._poll_interval = 0.01

    subscription = await manager.create(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
    )
    await _await_cursor(store, subscription.subscription_id, "file:offset=8")

    # The file is replaced with a shorter one (size 4 < offset 8).
    session.transport._files[_APP_LOG] = b"new\n"
    with caplog.at_level(
        logging.WARNING, logger="incidentlens_control_plane.logs.subscriptions"
    ):
        await _await_cursor(store, subscription.subscription_id, "file:offset=4")

    records = store.search(LogSearchFilters(project_id="payments"), limit=10)
    assert any(record.message_redacted == "new" for record in records)
    assert any("log source rotated" in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_failed_append_batch_does_not_advance_cursor(
    manager: LogSubscriptionManager, store, target_registration
) -> None:
    session = await manager._service._sessions.connect(target_registration)
    session.transport._files[_APP_LOG] = b"boom\n"
    manager._poll_interval = 0.01

    subscription = await manager.create(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
    )
    await _await_cursor(store, subscription.subscription_id, "file:offset=5")
    assert len(store.search(LogSearchFilters(project_id="payments"), limit=10)) == 1

    # Grow the file and make persistence fail from here on.
    session.transport._files[_APP_LOG] = b"boom\nfail\n"

    def failing_append(records):
        raise RuntimeError("db unavailable")

    store.append_batch = failing_append
    await asyncio.sleep(0.05)  # let several polls attempt and fail

    assert store.get_cursor(subscription.subscription_id).cursor == "file:offset=5"
    assert len(store.search(LogSearchFilters(project_id="payments"), limit=10)) == 1


@pytest.mark.asyncio
async def test_docker_backpressure_closes_process_and_emits_safe_event(
    manager: LogSubscriptionManager, runtime_events
) -> None:
    subscription = await manager.create(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.DOCKER,
        scope=LogScope.CONTAINER,
        source_ref="payments-api-1",
        opt_in_streaming=True,
        created_by="alice",
    )
    await manager.force_backpressure_for_test(subscription.subscription_id)

    events = runtime_events.list_after(0, limit=100)
    payloads = [
        event.payload
        for event in events
        if event.event_type.value == "log.backpressure"
    ]

    assert payloads
    assert "token=abc123" not in json.dumps(payloads)
    assert "dev-a.example.test" not in json.dumps(payloads)


@pytest.mark.asyncio
async def test_repeated_errors_move_subscription_to_error_with_redacted_summary(
    manager: LogSubscriptionManager, store
) -> None:
    subscription = store.create_subscription(
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
    manager.max_failures = 2
    await manager.record_failure_for_test(
        subscription.subscription_id,
        RuntimeError("token=abc123 host dev-a.example.test"),
    )
    await manager.record_failure_for_test(
        subscription.subscription_id,
        RuntimeError("token=abc123 host dev-a.example.test"),
    )

    errored = store.get_subscription(subscription.subscription_id)
    assert errored.status.value == "error"
    assert "abc123" not in errored.last_error_redacted
    assert "dev-a.example.test" not in errored.last_error_redacted


@pytest.mark.asyncio
async def test_record_failure_for_absent_subscription_is_noop(
    manager: LogSubscriptionManager, store
) -> None:
    manager.max_failures = 1
    await manager.record_failure_for_test("ghost-sub", RuntimeError("boom"))

    assert store.get_subscription("ghost-sub") is None


@pytest.mark.asyncio
async def test_record_failure_for_deleted_subscription_is_noop(
    manager: LogSubscriptionManager, store
) -> None:
    subscription = store.create_subscription(
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
    store.delete_subscription(
        subscription.subscription_id, now=datetime(2026, 8, 12, tzinfo=UTC)
    )
    manager.max_failures = 1
    await manager.record_failure_for_test(
        subscription.subscription_id, RuntimeError("boom")
    )

    assert store.get_subscription(subscription.subscription_id).status.value == "deleted"


@pytest.mark.asyncio
async def test_errored_subscription_resume_restarts_and_resets_failures(
    manager: LogSubscriptionManager, store, runtime_events
) -> None:
    subscription = await manager.create(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
    )
    manager.max_failures = 2
    await manager.record_failure_for_test(
        subscription.subscription_id, RuntimeError("boom one")
    )
    await manager.record_failure_for_test(
        subscription.subscription_id, RuntimeError("boom two")
    )

    assert store.get_subscription(subscription.subscription_id).status.value == "error"
    assert subscription.subscription_id not in manager.running_subscription_ids()

    resumed = await manager.resume(subscription.subscription_id)

    assert resumed.status.value == "active"
    assert subscription.subscription_id in manager.running_subscription_ids()
    events = runtime_events.list_after(0, limit=100)
    assert any(
        event.event_type.value == "log.subscription_resumed" for event in events
    )

    # The failure counter was reset on resume: a single failure must not
    # immediately re-error the subscription (needs 2 with max_failures=2).
    await manager.record_failure_for_test(
        subscription.subscription_id, RuntimeError("one more")
    )
    assert store.get_subscription(subscription.subscription_id).status.value == "active"


@pytest.mark.asyncio
async def test_writer_publishes_live_records_to_subscribers(
    manager: LogSubscriptionManager, store, target_registration
) -> None:
    session = await manager._service._sessions.connect(target_registration)
    session.transport._files[_APP_LOG] = b"live line\n"
    manager._poll_interval = 0.01

    subscription = store.create_subscription(
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

    async with manager.subscribe_records(subscription.subscription_id) as queue:
        await manager.start_active_opt_in()
        await _await_cursor(store, subscription.subscription_id, "file:offset=10")
        record = await asyncio.wait_for(queue.get(), timeout=2)

    assert record.message_redacted == "live line"
    assert record.subscription_id == subscription.subscription_id


@pytest.mark.asyncio
async def test_pause_rejects_already_paused_subscription(
    manager: LogSubscriptionManager, store
) -> None:
    subscription = store.create_subscription(
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
    await manager.pause(subscription.subscription_id)

    with pytest.raises(InvalidSubscriptionTransition):
        await manager.pause(subscription.subscription_id)


@pytest.mark.asyncio
async def test_resume_rejects_active_subscription(
    manager: LogSubscriptionManager, store
) -> None:
    subscription = store.create_subscription(
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

    with pytest.raises(InvalidSubscriptionTransition):
        await manager.resume(subscription.subscription_id)


@pytest.mark.asyncio
async def test_subscription_runs_are_audited_start_and_stop(
    manager: LogSubscriptionManager, tmp_path
) -> None:
    subscription = await manager.create(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
    )
    await manager.pause(subscription.subscription_id)

    with sqlite3.connect(tmp_path / "runtime.db") as conn:
        rows = conn.execute(
            "SELECT subscription_id, status, stopped_at FROM log_subscription_runs"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0][0] == subscription.subscription_id
    assert rows[0][1] == "completed"
    assert rows[0][2] is not None


@pytest.mark.asyncio
async def test_error_run_audits_redacted_summary(
    manager: LogSubscriptionManager, tmp_path
) -> None:
    subscription = await manager.create(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
    )
    manager.max_failures = 1
    await manager.record_failure_for_test(
        subscription.subscription_id,
        RuntimeError("token=abc123 host dev-a.example.test"),
    )

    with sqlite3.connect(tmp_path / "runtime.db") as conn:
        rows = conn.execute(
            "SELECT status, error FROM log_subscription_runs"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0][0] == "error"
    assert "abc123" not in rows[0][1]
    assert "dev-a.example.test" not in rows[0][1]


@pytest.mark.asyncio
async def test_docker_backpressure_reconnect_has_exponential_backoff(
    manager: LogSubscriptionManager,
    store,
    target_registration,
    monkeypatch,
) -> None:
    subscription = await manager.create(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.DOCKER,
        scope=LogScope.CONTAINER,
        source_ref="payments-api-1",
        opt_in_streaming=True,
        created_by="alice",
    )
    # Stop the manager's own reader/writer and drive _stream_docker directly
    # with a queue that stays full so the writer can never drain it.
    await manager._stop(subscription.subscription_id)
    session = await manager._service._sessions.connect(target_registration)
    session.transport.process_chunks = [
        b"line one\n",
        b"line two\n",
        b"line three\n",
    ]
    manager._queue_put_timeout = 0.01
    queue: asyncio.Queue[_QueuedLine] = asyncio.Queue(maxsize=1)
    raw = RawLogLine(
        source_ref="payments-api-1",
        cursor="docker:time=2026-08-12T10:00:00Z:seq=1",
        observed_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        text="occupied",
    )
    await queue.put(_QueuedLine(raw=raw, generation="g"))

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            manager._stream_docker(subscription, queue), timeout=0.2
        )

    assert len(sleeps) >= 2
    assert sleeps[0] == 1.0
    assert sleeps[1] == 2.0
