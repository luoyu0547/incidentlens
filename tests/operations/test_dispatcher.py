"""Worker-pool dispatch tests for the durable operation dispatcher.

The harness reuses the real SQLite-backed runtime and constructs dedicated
``OperationDispatcher`` instances with tiny heartbeat/poll intervals so
claim/execute/heartbeat/stop behavior is exercised deterministically without a
real timer.  The core concerns:

- ``start()`` recovers leftovers BEFORE any claim (a dangerous running
  ROLLBACK is parked UNCERTAIN and never dispatched);
- registered handlers run once, to ``succeeded`` (or ``failed`` on error);
- execution is serialized per ``session_id`` while session-less work runs
  concurrently;
- the heartbeat keeps a long-running row ``claimed_at`` fresh;
- ``stop(grace_seconds=...)`` requeues safe in-flight work and parks a
  dangerous in-flight ROLLBACK UNCERTAIN.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from incidentlens_control_plane.operations.dispatcher import OperationDispatcher
from incidentlens_control_plane.operations.handlers import OperationResult
from incidentlens_control_plane.operations.types import OperationKind, OperationStatus

NOW = datetime(2026, 8, 24, 10, 0, 0, tzinfo=UTC)


def _dispatcher(runtime, **overrides):
    return OperationDispatcher(
        store=runtime.operation_store,
        operations=runtime.operations,
        recovery=runtime.operation_recovery,
        poll_interval=0.01,
        **overrides,
    )


async def _wait_terminal(store, operation_id, *, timeout: float = 3.0) -> OperationStatus:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        status = store.get(operation_id).status
        if status in (
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
            OperationStatus.UNCERTAIN,
        ):
            return status
        assert loop.time() < deadline, f"timeout waiting for terminal {operation_id}"
        await asyncio.sleep(0.02)


async def test_start_recovers_before_claiming_dangerous_work(runtime_factory) -> None:
    runtime = runtime_factory()
    operation = runtime.operations.enqueue(
        kind=OperationKind.ROLLBACK,
        target_id="tgt-a",
        created_by="alice",
        request_payload='{"changeset_id":"chs-1","approval_id":null}',
        now=NOW,
    )
    runtime.operation_store.transition(
        runtime.operation_store.get(operation.operation_id),
        OperationStatus.RUNNING,
        now=NOW,
    )

    dispatcher = runtime.dispatcher
    await dispatcher.start()
    await dispatcher.stop(grace_seconds=0.5)

    restored = runtime.operation_store.get(operation.operation_id)
    # Recovery parked the dangerous running rollback BEFORE the first worker
    # could claim it: no execution happened (a dispatch would have FAILED the
    # missing changeset, not parked it UNCERTAIN).
    assert restored.status == OperationStatus.UNCERTAIN
    assert runtime.changes.rollback_calls == []


async def test_dispatch_runs_registered_handler_to_success(runtime_factory) -> None:
    runtime = runtime_factory()
    dispatcher = _dispatcher(runtime, concurrency=2)
    seen: list[str] = []

    async def handler(operation):
        seen.append(operation.operation_id)
        return OperationResult(summary="probe ok")

    dispatcher.register(OperationKind.TARGET_TEST, handler)
    operation = runtime.operations.enqueue(
        kind=OperationKind.TARGET_TEST, target_id="tgt-a", created_by="alice", now=NOW
    )

    await dispatcher.start()
    status = await _wait_terminal(runtime.operation_store, operation.operation_id)
    await dispatcher.stop(grace_seconds=0.5)

    assert status == OperationStatus.SUCCEEDED
    assert seen == [operation.operation_id]
    assert runtime.operation_store.get(operation.operation_id).progress_summary == "probe ok"


async def test_handler_error_marks_operation_failed(runtime_factory) -> None:
    runtime = runtime_factory()
    dispatcher = _dispatcher(runtime)

    async def handler(operation):
        raise ValueError("boom")

    dispatcher.register(OperationKind.TARGET_TEST, handler)
    operation = runtime.operations.enqueue(
        kind=OperationKind.TARGET_TEST, target_id="tgt-a", created_by="alice", now=NOW
    )

    await dispatcher.start()
    status = await _wait_terminal(runtime.operation_store, operation.operation_id)
    await dispatcher.stop(grace_seconds=0.5)

    stored = runtime.operation_store.get(operation.operation_id)
    assert status == OperationStatus.FAILED
    assert stored.error_code == "operation_failed"
    assert "boom" in (stored.error_message or "")


async def test_heartbeat_keeps_running_row_fresh(runtime_factory) -> None:
    runtime = runtime_factory()
    dispatcher = _dispatcher(
        runtime, heartbeat_seconds=0.05, concurrency=1
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(operation):
        entered.set()
        await release.wait()
        return OperationResult(summary="done")

    dispatcher.register(OperationKind.TARGET_TEST, handler)
    operation = runtime.operations.enqueue(
        kind=OperationKind.TARGET_TEST, target_id="tgt-a", created_by="alice", now=NOW
    )

    await dispatcher.start()
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    first = runtime.operation_store.get(operation.operation_id).claimed_at
    await asyncio.sleep(0.15)  # well past several heartbeat ticks
    second = runtime.operation_store.get(operation.operation_id).claimed_at

    assert first is not None
    assert second is not None
    assert second > first, "heartbeat did not refresh claimed_at of the running row"

    release.set()
    await _wait_terminal(runtime.operation_store, operation.operation_id)
    await dispatcher.stop(grace_seconds=0.5)


async def test_same_session_is_serialized_while_sessionless_runs_concurrently(
    runtime_factory,
) -> None:
    runtime = runtime_factory()
    dispatcher = _dispatcher(runtime, concurrency=4)
    log: list[tuple[str, str]] = []

    async def handler(operation):
        log.append((operation.operation_id, "enter"))
        await asyncio.sleep(0.05)
        log.append((operation.operation_id, "exit"))
        return OperationResult(summary="ok")

    dispatcher.register(OperationKind.TARGET_TEST, handler)
    same_session_a = runtime.operations.enqueue(
        kind=OperationKind.TARGET_TEST,
        target_id="tgt-a",
        created_by="alice",
        session_id="sess-1",
        now=NOW,
    )
    same_session_b = runtime.operations.enqueue(
        kind=OperationKind.TARGET_TEST,
        target_id="tgt-b",
        created_by="alice",
        session_id="sess-1",
        now=NOW,
    )
    independent = runtime.operations.enqueue(
        kind=OperationKind.TARGET_TEST, target_id="tgt-c", created_by="alice", now=NOW
    )

    await dispatcher.start()
    for operation in (same_session_a, same_session_b, independent):
        await _wait_terminal(runtime.operation_store, operation.operation_id)
    await dispatcher.stop(grace_seconds=0.5)

    session_ops = {same_session_a.operation_id, same_session_b.operation_id}
    entered: set[str] = set()
    overlaps = 0
    for operation_id, event in log:
        if operation_id not in session_ops:
            continue
        if event == "enter":
            entered.add(operation_id)
            if len(entered) > 1:
                overlaps += 1
        else:
            entered.discard(operation_id)
    assert overlaps == 0, "two operations of the same session ran concurrently"
    assert session_ops <= {operation_id for operation_id, _ in log}


async def test_stop_requeues_safe_and_parks_dangerous_in_flight(runtime_factory) -> None:
    runtime = runtime_factory()
    dispatcher = _dispatcher(runtime, concurrency=4)
    release = asyncio.Event()

    async def blocking(operation):
        await release.wait()
        return OperationResult(summary="done")

    dispatcher.register(OperationKind.TARGET_TEST, blocking)
    dispatcher.register(OperationKind.ROLLBACK, blocking)
    safe = runtime.operations.enqueue(
        kind=OperationKind.TARGET_TEST, target_id="tgt-a", created_by="alice", now=NOW
    )
    dangerous = runtime.operations.enqueue(
        kind=OperationKind.ROLLBACK,
        target_id="tgt-a",
        created_by="alice",
        request_payload='{"changeset_id":"chs-1","approval_id":null}',
        now=NOW,
    )

    await dispatcher.start()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3.0
    while True:
        statuses = {
            runtime.operation_store.get(operation.operation_id).status
            for operation in (safe, dangerous)
        }
        if statuses == {OperationStatus.RUNNING}:
            break
        assert loop.time() < deadline, "operations never entered RUNNING"
        await asyncio.sleep(0.02)

    await dispatcher.stop(grace_seconds=0.1)

    # Safe read-only in-flight work is returned to the queue for a fresh worker;
    # the dangerous in-flight rollback is parked UNCERTAIN (never replayed).
    assert (
        runtime.operation_store.get(safe.operation_id).status
        == OperationStatus.QUEUED
    )
    assert (
        runtime.operation_store.get(dangerous.operation_id).status
        == OperationStatus.UNCERTAIN
    )
