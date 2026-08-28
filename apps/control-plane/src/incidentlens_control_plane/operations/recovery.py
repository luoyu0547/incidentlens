"""Startup classification of durable operations after a process restart.

``OperationRecovery`` mirrors the investigation ``RecoveryService`` philosophy
for the durable operations table: after a restart every non-terminal
:class:`Operation` is classified by kind so a restart can never re-execute an
operation whose outcome is unconfirmed.

The rules are deliberately conservative:

- a ``running`` ROLLBACK (dangerous, remote-mutating) becomes ``uncertain`` and
  is never auto-replayed -- the operator decides what happened on the target;
- a ``running`` TARGET_TEST (read-only) is requeued to ``queued`` where a fresh
  worker may safely re-run it;
- ``queued`` work survives untouched;
- agent-kind operations (``agent_message`` / ``investigation_start``) are
  reconciled against their linked investigation/run; when the linked work is
  terminal the operation is marked accordingly, otherwise (and only while the
  linked investigation still exists and is live) it is requeued.  An orphan
  whose linked investigation no longer exists is marked ``failed`` so it reaches
  a terminal state instead of requeueing forever with no dispatcher handler;
- a ``running`` REPORT_GENERATE is requeued -- report content is derived
  deterministically and there is no durable half-written result to resolve;
- any leftover ``cancel_requested`` row is finalised to ``cancelled``.

``recover`` MUST be called only at startup, BEFORE the dispatcher starts its
workers (never while a dispatcher is alive): it requeues/UNCERTAINs every
``running`` row, and doing that to an operation a live worker is actively
executing would be wrong.  The caller (the dispatcher's ``start()``) guarantees
this ordering.  For the same reason ``recover`` is idempotent on an
already-consistent store -- every transition is state-machine-validated and
best-effort, and an empty pass returns a zero summary.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from incidentlens_control_plane.investigation.state_machine import (
    AGENT_RUN_STATE_MACHINE,
    INVESTIGATION_STATE_MACHINE,
    AgentRunStatus,
    IllegalTransition,
    InvestigationStatus,
)
from incidentlens_control_plane.investigation.store import (
    InvestigationNotFound,
    InvestigationStore,
)
from incidentlens_control_plane.operations.service import OperationService
from incidentlens_control_plane.operations.store import (
    ConcurrentOperationUpdate,
    OperationStore,
)
from incidentlens_control_plane.operations.types import Operation, OperationKind, OperationStatus

logger = logging.getLogger(__name__)

#: Kinds whose execution mutates remote state.  A ``running`` row of one of these
#: is never auto-replayed after a restart -- its outcome cannot be confirmed.
_DANGEROUS_RUNNING_KINDS: frozenset[OperationKind] = frozenset(
    {OperationKind.ROLLBACK}
)

#: Kinds that proxy an agent loop rather than execute directly in the worker.
_AGENT_KINDS: frozenset[OperationKind] = frozenset(
    {OperationKind.AGENT_MESSAGE, OperationKind.INVESTIGATION_START}
)


@dataclass(frozen=True)
class OperationRecoverySummary:
    """The result of one operation recovery pass."""

    scanned: int = 0
    uncertain_rollbacks: int = 0
    requeued_target_tests: int = 0
    reconciled_agent_operations: int = 0
    requeued_reports: int = 0
    finalised_cancels: int = 0


class OperationRecovery:
    """Classify every non-terminal operation after a restart."""

    def __init__(
        self,
        *,
        store: OperationStore,
        operations: OperationService,
        investigations: InvestigationStore,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._operations = operations
        self._investigations = investigations
        self._now = now or (lambda: datetime.now(UTC))

    async def recover(self, *, now: datetime) -> OperationRecoverySummary:
        """Classify every non-terminal operation against the per-kind rules."""
        summary = OperationRecoverySummary()
        for operation in self._store.list_non_terminal():
            summary = replace(summary, scanned=summary.scanned + 1)
            decision = self._classify(operation, now=now)
            if decision is None:
                continue
            key, _count = decision
            summary = replace(summary, **{key: getattr(summary, key) + 1})
        return summary

    def _classify(
        self, operation: Operation, *, now: datetime
    ) -> tuple[str, int] | None:
        """Apply the per-kind rule to one non-terminal row.

        Returns ``(summary_field, increment)`` when a decision changed the row,
        else ``None``.  Every state-machine move is best-effort: a concurrent
        writer that already moved the row is ignored.
        """
        status = operation.status
        if status is OperationStatus.QUEUED:
            # Queued work survives a restart untouched; the dispatcher claims it.
            return None
        if status is OperationStatus.CANCEL_REQUESTED:
            # Crash mid-cancel: finalise the cancellation.
            if self._transition(
                operation,
                OperationStatus.CANCELLED,
                progress_summary="cancellation finalised after restart",
                now=now,
            ):
                return ("finalised_cancels", 1)
            return None
        if status is not OperationStatus.RUNNING:
            return None

        if operation.kind in _DANGEROUS_RUNNING_KINDS:
            if self._transition(
                operation,
                OperationStatus.UNCERTAIN,
                progress_summary=(
                    "interrupted by restart; remote outcome cannot be confirmed "
                    "and is never replayed"
                ),
                now=now,
            ):
                return ("uncertain_rollbacks", 1)
            return None
        if operation.kind is OperationKind.TARGET_TEST:
            if self._requeue(operation, now=now):
                return ("requeued_target_tests", 1)
            return None
        if operation.kind in _AGENT_KINDS:
            if self._reconcile_agent_kind(operation, now=now):
                return ("reconciled_agent_operations", 1)
            return None
        if operation.kind is OperationKind.REPORT_GENERATE:
            if self._requeue(operation, now=now):
                return ("requeued_reports", 1)
            return None

        # Unknown / future kind: park uncertain so it is never replayed blindly.
        if self._transition(
            operation,
            OperationStatus.UNCERTAIN,
            progress_summary="unknown operation kind left over after restart; parked uncertain",
            now=now,
        ):
            return ("uncertain_rollbacks", 1)  # counted under the uncertain bucket
        return None

    def _reconcile_agent_kind(self, operation: Operation, *, now: datetime) -> bool:
        """Reconcile an agent-kind operation from its linked investigation/run.

        When the investigation (or every linked run) is terminal the operation is
        marked to match; an orphan whose linked investigation no longer exists is
        marked ``failed`` so it reaches a terminal state (no dispatcher handler
        would ever claim it); only while the linked investigation exists and is
        still live is the operation requeued for the agent loop.
        Returns ``True`` when a decision moved the operation (terminal or requeued).
        """
        if operation.investigation_id is None:
            return self._transition(
                operation,
                OperationStatus.FAILED,
                progress_summary=(
                    "orphaned agent operation: no linked investigation to reconcile"
                ),
                now=now,
            )
        try:
            investigation = self._investigations.get_investigation(
                operation.investigation_id
            )
        except InvestigationNotFound:
            # The linked investigation is gone (or never existed); there is no
            # live agent loop to hand back to, so park the operation failed
            # rather than requeueing it forever without a dispatcher handler.
            return self._transition(
                operation,
                OperationStatus.FAILED,
                progress_summary=(
                    f"orphaned agent operation: linked investigation "
                    f"{operation.investigation_id} no longer exists"
                ),
                now=now,
            )

        runs = self._investigations.list_agent_runs(
            investigation_id=operation.investigation_id
        )
        if INVESTIGATION_STATE_MACHINE.is_terminal(investigation.status):
            target = (
                OperationStatus.SUCCEEDED
                if investigation.status is InvestigationStatus.COMPLETED
                else OperationStatus.FAILED
            )
            return self._transition(
                operation,
                target,
                progress_summary=f"linked investigation {operation.investigation_id} is terminal",
                now=now,
            )
        if runs and all(
            AGENT_RUN_STATE_MACHINE.is_terminal(run.status) for run in runs
        ):
            target = (
                OperationStatus.SUCCEEDED
                if any(run.status is AgentRunStatus.COMPLETED for run in runs)
                else OperationStatus.FAILED
            )
            return self._transition(
                operation,
                target,
                progress_summary=f"linked runs of {operation.investigation_id} are terminal",
                now=now,
            )
        # The linked work is still live; hand the operation back to the queue.
        return self._requeue(operation, now=now)

    # -- best-effort store moves ----------------------------------------------

    def _transition(
        self,
        operation: Operation,
        target: OperationStatus,
        *,
        progress_summary: str | None,
        now: datetime,
    ) -> bool:
        try:
            self._operations.transition(
                operation.operation_id,
                target,
                progress_summary=progress_summary,
                now=now,
            )
            return True
        except (IllegalTransition, ConcurrentOperationUpdate):
            return False

    def _requeue(self, operation: Operation, *, now: datetime) -> bool:
        try:
            self._operations.requeue(operation.operation_id, now=now)
            return True
        except (IllegalTransition, ConcurrentOperationUpdate):
            return False


__all__ = ["OperationRecovery", "OperationRecoverySummary"]
