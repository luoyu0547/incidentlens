from datetime import UTC, datetime

import pytest
from incidentlens_control_plane.investigation.hooks import (
    HookEvent,
    HookEventType,
    HookRunner,
)


def hook_event(event_type: HookEventType) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        agent_run_id="run-1",
        action_name="host.read",
        occurred_at=datetime(2026, 8, 18, tzinfo=UTC),
    )


def raising_callback(event: HookEvent) -> None:
    raise RuntimeError("secret: should be redacted")


@pytest.mark.asyncio
async def test_hook_runner_calls_registered_callbacks_in_order() -> None:
    seen: list[str] = []
    runner = HookRunner()
    runner.register(HookEventType.PRE_TOOL_USE, lambda event: seen.append("first"))
    runner.register(HookEventType.PRE_TOOL_USE, lambda event: seen.append("second"))

    failures = await runner.emit(hook_event(HookEventType.PRE_TOOL_USE))

    assert seen == ["first", "second"]
    assert failures == ()


@pytest.mark.asyncio
async def test_hook_failure_is_returned_not_raised() -> None:
    runner = HookRunner()
    runner.register(HookEventType.PRE_TOOL_USE, raising_callback)

    failures = await runner.emit(hook_event(HookEventType.PRE_TOOL_USE))

    assert len(failures) == 1
    assert "secret" not in failures[0]
    assert len(failures[0]) <= 500


@pytest.mark.asyncio
async def test_async_hook_is_awaited() -> None:
    seen: list[str] = []

    async def callback(event: HookEvent) -> None:
        seen.append(event.action_name)

    runner = HookRunner()
    runner.register(HookEventType.POST_TOOL_USE, callback)

    assert await runner.emit(hook_event(HookEventType.POST_TOOL_USE)) == ()
    assert seen == ["host.read"]
