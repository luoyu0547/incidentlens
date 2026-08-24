"""The operation dispatcher: claim, run, heartbeat and complete durable work.

``OperationDispatcher`` turns queued :class:`Operation` rows into executed work.
It owns the worker lifecycle and the claim/heartbeat/terminal-transition path;
registered handlers (see :mod:`operations.handlers`) own the per-kind execution.

Lifecycle:

- ``start()`` first runs :class:`OperationRecovery` (so leftovers from a crash
  are classified BEFORE any new work is claimed), then launches a bounded pool
  of worker coroutines.
- Each worker atomically claims one ``queued`` operation for a registered kind,
  runs its handler, and moves the operation to ``succeeded``/``failed``.
- Execution is serialized per ``session_id`` (one active operation per session
  at a time) while independent session-less operations (notably TARGET_TEST)
  run concurrently across workers.
- While a handler runs, a heartbeat task touches ``claimed_at`` every
  ``heartbeat_seconds`` so a live operation is never misread as a crash-stale
  ``running`` row.

``stop(grace_seconds=...)`` stops claiming new work, gives in-flight handlers a
grace window to drain, cancels whatever is still running and then sweeps any
leftover ``running`` rows the same conservative way ``OperationRecovery`` does:
a dangerous ROLLBACK is parked ``uncertain`` (never replayed), safe read-only
work is requeued.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from incidentlens_control_plane.investigation.state_machine import IllegalTransition
from incidentlens_control_plane.operations.handlers import (
    OperationHandler,
    OperationResult,
)
from incidentlens_control_plane.operations.recovery import OperationRecovery
from incidentlens_control_plane.operations.service import OperationService
from incidentlens_control_plane.operations.store import (
    ConcurrentOperationUpdate,
    OperationNotClaimable,
    OperationNotFound,
    OperationStore,
)
from incidentlens_control_plane.operations.types import Operation, OperationKind, OperationStatus

logger = logging.getLogger(__name__)

#: Default worker heartbeat interval (keep a live running row fresh).
_DEFAULT_HEARTBEAT_SECONDS = 10.0
#: Running rows untouched for longer than this are stale on startup.
_DEFAULT_STALE_AFTER_SECONDS = 30.0
#: Worker poll cadence when the queue is empty.
_DEFAULT_POLL_INTERVAL = 0.5
#: Number of concurrent worker coroutines (bounded by per-session serialization).
_DEFAULT_CONCURRENCY = 4


class OperationDispatcher:
    """Claim queued operations, run their handlers and finalize them."""

    def __init__(
        self,
        *,
        store: OperationStore,
        operations: OperationService,
        recovery: OperationRecovery,
        heartbeat_seconds: float = _DEFAULT_HEARTBEAT_SECONDS,
        stale_after_seconds: float = _DEFAULT_STALE_AFTER_SECONDS,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        concurrency: int = _DEFAULT_CONCURRENCY,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if heartbeat_seconds <= 0 or stale_after_seconds <= 0:
            raise ValueError("heartbeat/stale intervals must be positive")
        if not (1 <= concurrency <= 64):
            raise ValueError("concurrency must be between 1 and 64")
        self._store = store
        self._operations = operations
        self._recovery = recovery
        self._heartbeat_seconds = heartbeat_seconds
        self._stale_after_seconds = stale_after_seconds
        self._poll_interval = poll_interval
        self._concurrency = concurrency
        self._now = now or (lambda: datetime.now(UTC))
        self._handlers: dict[OperationKind, OperationHandler] = {}
        self._active = False
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._in_flight: dict[str, Operation] = {}
        self._active_sessions: set[str] = set()
        self._selection_lock = asyncio.Lock()

    # -- registration ---------------------------------------------------------

    def register(self, kind: OperationKind, handler: OperationHandler) -> None:
        """Register the worker-side handler for one operation kind."""
        self._handlers[kind] = handler

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Recover leftovers, then start the worker pool."""
        if self._active:
            return
        # Recovery runs BEFORE any claim so a crash-stale dangerous row is
        # never replayed by the first worker tick.
        await self._recovery.recover(now=self._now())
        self._active = True
        for worker_id in range(self._concurrency):
            self._worker_tasks.append(
                asyncio.create_task(self._worker(worker_id))
            )

    async def stop(self, *, grace_seconds: float) -> None:
        """Stop claiming, drain in-flight work within *grace_seconds*, then sweep."""
        self._active = False
        workers = list(self._worker_tasks)
        self._worker_tasks.clear()
        if workers:
            _done, pending = await asyncio.wait(workers, timeout=grace_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._sweep_running()

    # -- worker loop ----------------------------------------------------------

    async def _worker(self, worker_id: int) -> None:
        worker_name = f"dispatcher:{worker_id}"
        while self._active:
            operation = await self._claim_next(worker_name)
            if operation is None:
                await asyncio.sleep(self._poll_interval)
                continue
            await self._execute(operation)

    async def _claim_next(self, worker_name: str) -> Operation | None:
        """Atomically claim one eligible queued operation, or ``None``."""
        async with self._selection_lock:
            if not self._active:
                return None
            for operation in self._store.list_queued(limit=100):
                if operation.kind not in self._handlers:
                    continue
                if (
                    operation.session_id is not None
                    and operation.session_id in self._active_sessions
                ):
                    continue
                try:
                    claimed = self._operations.claim(
                        operation.operation_id,
                        worker=worker_name,
                        now=self._now(),
                    )
                except (OperationNotClaimable, OperationNotFound):
                    continue
                if claimed.status is not OperationStatus.RUNNING:
                    continue
                self._in_flight[claimed.operation_id] = claimed
                if claimed.session_id is not None:
                    self._active_sessions.add(claimed.session_id)
                return claimed
        return None

    async def _execute(self, operation: Operation) -> None:
        """Run one claimed operation through its handler and finalize it."""
        heartbeat_task = asyncio.create_task(
            self._heartbeat(operation.operation_id)
        )
        try:
            handler = self._handlers[operation.kind]
            result: OperationResult = await handler(operation)
            if result.error_code is not None:
                self._operations.transition(
                    operation.operation_id,
                    OperationStatus.FAILED,
                    progress_summary=result.summary,
                    error_code=result.error_code,
                    error_message=result.error_message or result.error_code,
                    now=self._now(),
                )
            else:
                self._operations.transition(
                    operation.operation_id,
                    OperationStatus.SUCCEEDED,
                    progress_summary=result.summary,
                    now=self._now(),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a handler failure is FAILED
            logger.warning(
                "operation %s (%s) handler failed: %s",
                operation.operation_id,
                operation.kind.value,
                exc,
            )
            self._safe_transition_failed(operation, exc)
        finally:
            heartbeat_task.cancel()
            self._release(operation)

    async def _heartbeat(self, operation_id: str) -> None:
        """Keep a running row fresh until the operation reaches a terminal state."""
        try:
            while True:
                await asyncio.sleep(self._heartbeat_seconds)
                touched = self._operations.heartbeat(
                    operation_id, now=self._now()
                )
                if not touched:
                    return
        except asyncio.CancelledError:
            return

    def _safe_transition_failed(
        self, operation: Operation, exc: Exception
    ) -> None:
        try:
            self._operations.transition(
                operation.operation_id,
                OperationStatus.FAILED,
                progress_summary="handler failed",
                error_code="operation_failed",
                error_message=f"{type(exc).__name__}: {exc}",
                now=self._now(),
            )
        except (IllegalTransition, ConcurrentOperationUpdate):
            pass

    def _release(self, operation: Operation) -> None:
        self._in_flight.pop(operation.operation_id, None)
        if operation.session_id is not None:
            self._active_sessions.discard(operation.session_id)

    # -- shutdown sweep -------------------------------------------------------

    def _sweep_running(self) -> None:
        """Finalise any running rows that never drained within the grace window."""
        now = self._now()
        for operation in self._store.list_non_terminal():
            if operation.status is not OperationStatus.RUNNING:
                continue
            if operation.kind is OperationKind.ROLLBACK:
                try:
                    self._operations.transition(
                        operation.operation_id,
                        OperationStatus.UNCERTAIN,
                        progress_summary=(
                            "interrupted by shutdown; remote outcome cannot be "
                            "confirmed and is never replayed"
                        ),
                        now=now,
                    )
                except (IllegalTransition, ConcurrentOperationUpdate):
                    logger.warning(
                        "could not park %s uncertain on shutdown",
                        operation.operation_id,
                    )
            else:
                try:
                    self._operations.requeue(operation.operation_id, now=now)
                except (IllegalTransition, ConcurrentOperationUpdate):
                    logger.warning(
                        "could not requeue %s on shutdown", operation.operation_id
                    )


__all__ = ["OperationDispatcher"]
