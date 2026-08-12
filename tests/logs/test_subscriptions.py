"""LogSubscriptionManager state machine, recovery, and cursor tests."""

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest
from incidentlens_control_plane.logs.store import LogSearchFilters
from incidentlens_control_plane.logs.subscriptions import (
    LogSubscriptionManager,
    TooManyActiveSubscriptions,
)
from incidentlens_control_plane.logs.types import LogScope, LogSourceKind

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
