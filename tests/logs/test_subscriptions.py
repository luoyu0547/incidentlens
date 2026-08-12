"""LogSubscriptionManager state machine, recovery, and cursor tests."""

from datetime import UTC, datetime

import pytest
from incidentlens_control_plane.logs.subscriptions import (
    LogSubscriptionManager,
    TooManyActiveSubscriptions,
)
from incidentlens_control_plane.logs.types import LogScope, LogSourceKind


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
