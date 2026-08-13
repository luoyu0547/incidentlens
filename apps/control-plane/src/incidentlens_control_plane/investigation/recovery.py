"""Startup recovery and orderly shutdown for the Phase 4 agent runtime.

``RecoveryService`` owns the process-level lifecycle of the investigation
stack:

* ``startup`` runs after log subscriptions are restored.  It (1) reconciles
  approvals that were already decided before the restart but never resolved
  against a waiting tool call or pending registry proposal, then (2) scans
  every non-terminal investigation and run.  A run that was in flight when the
  process died is classified by its in-flight tool calls: a dangerous (remote
  mutating) call is marked ``UNCERTAIN`` with UNCERTAIN_STATE evidence and the
  run is parked ``PAUSED_UNCERTAIN_STATE`` — it is never auto-replayed; a safe
  read-only call is marked ``FAILED`` (retryable) and the run stays resumable.
  ``CANCEL_REQUESTED`` runs left over from a crash mid-cancel are finalised to
  ``CANCELLED``.  Nothing is auto-resumed beyond the operator-visible approval
  decisions, so a restart can never re-run a dangerous operation by itself.

* ``shutdown`` is the orderly teardown: refuse new investigations, park every
  active investigation/run for cancellation, give active loops a grace window
  to checkpoint/drain and finalise, then sweep whatever remains: unconfirmable
  in-flight tool calls become ``UNCERTAIN``, every leftover run/investigation
  is finalised ``CANCELLED``.  The lifespan then closes log subscriptions and
  host sessions afterwards.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.approvals.types import ApprovalStatus
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.evidence.service import EvidenceService
from incidentlens_control_plane.investigation.events import (
    InvestigationEventPublisher,
)
from incidentlens_control_plane.investigation.orchestrator import AgentOrchestrator
from incidentlens_control_plane.investigation.service import InvestigationService
from incidentlens_control_plane.investigation.state_machine import (
    AGENT_RUN_STATE_MACHINE,
    INVESTIGATION_STATE_MACHINE,
    AgentRunStatus,
    IllegalTransition,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.tools import (
    TOOL_DOCKER_ACTION,
    TOOL_FILE_EDIT,
    TOOL_FILE_WRITE,
    TOOL_SHELL_EXEC,
)
from incidentlens_control_plane.investigation.types import (
    AgentRun,
    AgentRunKind,
    Investigation,
    StopReason,
    ToolCall,
)

logger = logging.getLogger(__name__)

# Tools whose execution can mutate remote state (shell, file mutations, docker
# actions).  An in-flight call of one of these is dangerous: the process died
# before the outcome was persisted, so the remote side may have partially
# applied it.  Such a call is never replayed — it is parked UNCERTAIN.  Every
# other tool is a safe read-only operation whose in-flight call may simply be
# failed and retried.
_DANGEROUS_TOOLS: frozenset[str] = frozenset(
    {
        TOOL_SHELL_EXEC,
        TOOL_FILE_EDIT,
        TOOL_FILE_WRITE,
        TOOL_DOCKER_ACTION,
    }
)

_DECIDED_APPROVAL_STATUSES: frozenset[ApprovalStatus] = frozenset(
    {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
)


def _is_terminal_run(status: AgentRunStatus) -> bool:
    return AGENT_RUN_STATE_MACHINE.is_terminal(status)


def _is_terminal_investigation(status: InvestigationStatus) -> bool:
    return INVESTIGATION_STATE_MACHINE.is_terminal(status)


@dataclass(frozen=True)
class RecoverySummary:
    """The result of one startup recovery pass."""

    reconciled_approvals: int = 0
    scanned_investigations: int = 0
    dangerous_parked: int = 0
    safe_repaired: int = 0
    cancel_finalised: int = 0


class RecoveryService:
    """Startup recovery and orderly shutdown for the investigation stack."""

    def __init__(
        self,
        *,
        store: InvestigationStore,
        investigations: InvestigationService,
        orchestrator: AgentOrchestrator,
        evidence: EvidenceService,
        approvals: ApprovalService,
        shutdown_grace_seconds: float = 10.0,
        now: Callable[[], datetime] | None = None,
        events: RuntimeEventStore | None = None,
        broker: RuntimeEventBroker | None = None,
    ) -> None:
        if shutdown_grace_seconds < 0.5:
            raise ValueError("shutdown_grace_seconds must be >= 0.5")
        self._store = store
        self._investigations = investigations
        self._orchestrator = orchestrator
        self._evidence = evidence
        self._approvals = approvals
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._events_pub = (
            InvestigationEventPublisher(events, broker)
            if events is not None and broker is not None
            else None
        )
        self._accepting = True

    @property
    def accepting(self) -> bool:
        """Whether new investigations are currently accepted."""
        return self._accepting

    # -- startup --------------------------------------------------------------

    async def startup(self) -> RecoverySummary:
        """Restore a safe, resumable state after a process restart."""
        self._accepting = True
        self._investigations.accepting = True
        pending = len(self._store.list_non_terminal_investigations())
        # C2/M1: finalise crash-mid-cancel runs FIRST (sweeping their in-flight
        # tool calls) so a decided approval can never re-execute a dangerous
        # operation against a run the operator cancelled.
        cancel_finalised = self._finalise_cancel_requested()
        reconciled = await self._reconcile_decided_approvals()
        scanned = self._classify_in_flight_runs()
        scanned = replace(
            scanned,
            reconciled_approvals=reconciled,
            cancel_finalised=cancel_finalised,
        )
        # Only audit a recovery that actually did something; an empty startup
        # is a no-op and must not inject recovery.* events into the stream.
        if self._events_pub is not None and (
            pending or reconciled or cancel_finalised
        ):
            self._events_pub.recovery_started(count=pending, occurred_at=self._now())
            self._events_pub.recovery_completed(
                count=scanned.scanned_investigations, occurred_at=self._now()
            )
        return scanned

    def _finalise_cancel_requested(self) -> int:
        """Finalise every cancelled investigation and its runs; sweep their calls.

        A crash mid-cancel can leave the investigation parked CANCEL_REQUESTED
        while some runs are still WAITING_APPROVAL/PAUSED/RUNNING (the cancel
        parks the investigation before it parks every run).  Every non-terminal
        run of a CANCEL_REQUESTED investigation is therefore swept (a dangerous
        RUNNING call becomes UNCERTAIN with evidence, waiting/planned calls are
        cancelled) and finalised to CANCELLED, so a decided approval can never
        re-execute a dangerous operation underneath a cancelled investigation.
        Returns the number of investigations finalised.
        """
        count = 0
        for investigation in self._store.list_non_terminal_investigations():
            cancelled = investigation.status is InvestigationStatus.CANCEL_REQUESTED
            for run in self._store.list_agent_runs(
                investigation_id=investigation.investigation_id
            ):
                if _is_terminal_run(run.status):
                    continue
                if run.status is not AgentRunStatus.CANCEL_REQUESTED and not cancelled:
                    continue
                self._sweep_run_tool_calls(run, investigation)
                self._transition_run_to_cancelled(run)
            current = self._store.get_investigation(investigation.investigation_id)
            if current.status is InvestigationStatus.CANCEL_REQUESTED:
                if self._transition_investigation_to_cancelled(current):
                    count += 1
        return count

    async def _reconcile_decided_approvals(self) -> int:
        """Resolve approvals decided before the restart but never handled.

        An approval that was granted/rejected while the process was down has no
        live handler, so the linkage is repaired here: ``handle_approval_decision``
        matches it to a WAITING_APPROVAL tool call or a pending registry
        proposal, executes/rejects the exact single-use intent, and resumes the
        parked run.  PENDING approvals are left for the operator.
        """
        reconciled = 0
        for approval in self._approvals.list():
            if approval.status not in _DECIDED_APPROVAL_STATUSES:
                continue
            try:
                outcome = await self._investigations.handle_approval_decision(
                    approval.approval_id, now=self._now()
                )
            except Exception:  # noqa: BLE001 - one bad approval must not block recovery
                logger.exception(
                    "startup reconciliation failed for approval %s",
                    approval.approval_id,
                )
                continue
            if outcome.matched != "none":
                reconciled += 1
        return reconciled

    def _classify_in_flight_runs(self) -> RecoverySummary:
        """Classify the in-flight tool calls of every surviving non-terminal run.

        ``CANCEL_REQUESTED`` runs were already finalised by
        ``_finalise_cancel_requested`` before this pass; every other run is
        left parked and only its RUNNING tool calls are repaired.
        """
        summary = RecoverySummary()
        for investigation in self._store.list_non_terminal_investigations():
            summary = replace(
                summary,
                scanned_investigations=summary.scanned_investigations + 1,
            )
            for run in self._store.list_agent_runs(
                investigation_id=investigation.investigation_id
            ):
                if _is_terminal_run(run.status):
                    continue
                parked, repaired = self._classify_in_flight(run, investigation)
                if parked:
                    summary = replace(
                        summary,
                        dangerous_parked=summary.dangerous_parked + parked,
                    )
                if repaired:
                    summary = replace(
                        summary,
                        safe_repaired=summary.safe_repaired + repaired,
                    )
                # WAITING_APPROVAL / WAITING_CHILDREN / PAUSED_* / CREATED runs
                # are safe to leave parked; the operator resumes them, and the
                # orchestrator re-discovers waiting children and re-establishes
                # their container sessions on resume.
        return summary

    def _classify_in_flight(
        self, run: AgentRun, investigation: Investigation
    ) -> tuple[int, int]:
        """Repair the in-flight tool calls of a run found after restart.

        Returns ``(dangerous_parked, safe_repaired)``.  A dangerous in-flight
        call is marked UNCERTAIN (with UNCERTAIN_STATE evidence); when the run
        itself was RUNNING (mid-loop when the process died) it is also parked
        PAUSED_UNCERTAIN_STATE — never replayed.  A safe read-only in-flight
        call is marked FAILED (retryable) and a RUNNING run stays resumable.
        """
        in_flight = self._store.list_tool_calls(
            agent_run_id=run.agent_run_id, status=ToolCallStatus.RUNNING
        )
        if not in_flight:
            return 0, 0
        dangerous = [call for call in in_flight if call.tool_name in _DANGEROUS_TOOLS]
        safe = [call for call in in_flight if call.tool_name not in _DANGEROUS_TOOLS]
        for call in dangerous:
            self._mark_tool_uncertain(
                call,
                run,
                investigation,
                "interrupted by restart; outcome cannot be confirmed and is never replayed",
            )
        for call in safe:
            self._mark_tool_failed_retryable(call)
        if dangerous and run.status is AgentRunStatus.RUNNING:
            self._park_uncertain(run, investigation)
        return len(dangerous), len(safe)

    # -- shutdown -------------------------------------------------------------

    async def shutdown(self) -> int:
        """Stop the investigation stack in order; returns investigations cancelled.

        Order: refuse new investigations -> park every active investigation and
        run as cancel-requested -> give active loops a grace window to
        observe/drain/finalise -> sweep leftovers (unconfirmable dangerous calls
        become UNCERTAIN, every leftover run/investigation becomes CANCELLED).
        The lifespan closes log subscriptions and host sessions after this.
        """
        self._accepting = False
        self._investigations.accepting = False
        parked = await self._investigations.cancel_all_active(now=self._now())
        await self._orchestrator.drain_active_loops(self._shutdown_grace_seconds)
        self._sweep_shutdown()
        return parked

    def _sweep_shutdown(self) -> int:
        """Finalise every remaining non-terminal investigation/run.

        Returns the number of investigations finalised to a terminal state.
        """
        cancelled = 0
        for investigation in self._store.list_non_terminal_investigations():
            for run in self._store.list_agent_runs(
                investigation_id=investigation.investigation_id
            ):
                if _is_terminal_run(run.status):
                    continue
                self._sweep_run_tool_calls(run, investigation)
                self._transition_run_to_cancelled(run)
            current = self._store.get_investigation(investigation.investigation_id)
            if not _is_terminal_investigation(current.status):
                if self._transition_investigation_to_cancelled(current):
                    cancelled += 1
        return cancelled

    def _sweep_run_tool_calls(
        self, run: AgentRun, investigation: Investigation
    ) -> None:
        """Mark every non-terminal tool call of *run* terminal.

        A RUNNING dangerous call (execution was in progress when the process
        died / was interrupted) becomes UNCERTAIN with UNCERTAIN_STATE evidence
        -- its outcome cannot be confirmed and it is never replayed.  A RUNNING
        safe read-only call is failed (retryable); waiting/planned calls that
        never ran are cancelled.
        """
        for call in self._store.list_tool_calls(
            agent_run_id=run.agent_run_id, status=ToolCallStatus.RUNNING
        ):
            if call.tool_name in _DANGEROUS_TOOLS:
                self._mark_tool_uncertain(
                    call,
                    run,
                    investigation,
                    "interrupted execution; outcome cannot be confirmed and is never replayed",
                )
            else:
                self._mark_tool_failed_retryable(call)
        for call in self._store.list_tool_calls(
            agent_run_id=run.agent_run_id,
            status=ToolCallStatus.WAITING_APPROVAL,
        ):
            self._transition_tool_call(
                call, ToolCallStatus.CANCELLED,
                error_redacted="stopped before the approval decision",
            )
        for call in self._store.list_tool_calls(
            agent_run_id=run.agent_run_id, status=ToolCallStatus.PLANNED
        ):
            self._transition_tool_call(
                call, ToolCallStatus.CANCELLED,
                error_redacted="stopped before execution",
            )

    def _transition_run_to_cancelled(self, run: AgentRun) -> bool:
        """Move *run* to CANCELLED through the legal transitions."""
        now = self._now()
        try:
            if run.status is AgentRunStatus.CANCELLED:
                return False
            if run.status is AgentRunStatus.CREATED:
                self._store.transition_agent_run_status(
                    run.agent_run_id,
                    AgentRunStatus.CANCELLED,
                    now=now,
                    stop_reason=StopReason.CANCELLED,
                )
            elif run.status is AgentRunStatus.CANCEL_REQUESTED:
                self._store.transition_agent_run_status(
                    run.agent_run_id,
                    AgentRunStatus.CANCELLED,
                    now=now,
                    stop_reason=StopReason.CANCELLED,
                )
            else:
                self._store.transition_agent_run_status(
                    run.agent_run_id,
                    AgentRunStatus.CANCEL_REQUESTED,
                    now=now,
                    stop_reason=StopReason.CANCELLED,
                )
                self._store.transition_agent_run_status(
                    run.agent_run_id,
                    AgentRunStatus.CANCELLED,
                    now=now,
                    stop_reason=StopReason.CANCELLED,
                )
            return True
        except IllegalTransition:
            # A concurrent finalizer already moved the run; nothing to do.
            return False

    def _transition_investigation_to_cancelled(self, investigation: Investigation) -> bool:
        now = self._now()
        try:
            if investigation.status is InvestigationStatus.CREATED:
                self._store.transition_investigation_status(
                    investigation.investigation_id,
                    InvestigationStatus.CANCELLED,
                    now=now,
                    stop_reason=StopReason.CANCELLED,
                )
            elif investigation.status is InvestigationStatus.CANCEL_REQUESTED:
                self._store.transition_investigation_status(
                    investigation.investigation_id,
                    InvestigationStatus.CANCELLED,
                    now=now,
                    stop_reason=StopReason.CANCELLED,
                )
            else:
                self._store.transition_investigation_status(
                    investigation.investigation_id,
                    InvestigationStatus.CANCEL_REQUESTED,
                    now=now,
                    stop_reason=StopReason.CANCELLED,
                )
                self._store.transition_investigation_status(
                    investigation.investigation_id,
                    InvestigationStatus.CANCELLED,
                    now=now,
                    stop_reason=StopReason.CANCELLED,
                )
            return True
        except IllegalTransition:
            return False

    # -- repair helpers -------------------------------------------------------

    def _park_uncertain(
        self, run: AgentRun, investigation: Investigation
    ) -> None:
        """Park a run (and a RUNNING parent investigation) as PAUSED_UNCERTAIN_STATE."""
        now = self._now()
        try:
            self._store.transition_agent_run_status(
                run.agent_run_id,
                AgentRunStatus.PAUSED_UNCERTAIN_STATE,
                now=now,
                stop_reason=StopReason.UNCERTAIN_STATE,
            )
        except IllegalTransition:
            pass
        if run.kind is AgentRunKind.PARENT:
            try:
                current = self._store.get_investigation(
                    investigation.investigation_id
                )
                if current.status is InvestigationStatus.RUNNING:
                    self._store.transition_investigation_status(
                        current.investigation_id,
                        InvestigationStatus.PAUSED_UNCERTAIN_STATE,
                        now=now,
                        stop_reason=StopReason.UNCERTAIN_STATE,
                    )
            except Exception:  # noqa: BLE001 - best effort
                pass

    def _mark_tool_uncertain(
        self,
        call: ToolCall,
        run: AgentRun,
        investigation: Investigation,
        reason: str,
    ) -> None:
        """Mark a dangerous in-flight call UNCERTAIN and record audit evidence."""
        now = self._now()
        try:
            self._evidence.record_uncertain_state(
                agent_run_id=run.agent_run_id,
                incident_id=investigation.incident_id,
                project_id=run.scope.project_id,
                target_id=run.scope.target_id,
                service_name=run.scope.service_name or "host",
                reason="unconfirmed_in_flight_tool",
                description=reason,
                source_ref=f"tool:{call.tool_call_id}",
                created_by="recovery",
                now=now,
            )
        except Exception:  # noqa: BLE001 - evidence must never block recovery
            logger.exception("failed to record uncertain evidence for %s", call.tool_call_id)
        self._transition_tool_call(
            call, ToolCallStatus.UNCERTAIN, error_redacted=reason
        )

    def _mark_tool_failed_retryable(self, call: ToolCall) -> None:
        """Mark a safe read-only in-flight call FAILED so the run can retry it."""
        self._transition_tool_call(
            call,
            ToolCallStatus.FAILED,
            error_redacted="interrupted by restart; safe read-only call, retryable",
        )

    def _transition_tool_call(
        self,
        call: ToolCall,
        target: ToolCallStatus,
        *,
        error_redacted: str | None = None,
    ) -> None:
        try:
            self._store.transition_tool_call_status(
                call.tool_call_id,
                target,
                now=self._now(),
                error_redacted=error_redacted,
            )
        except Exception:  # noqa: BLE001 - best effort sweep
            logger.warning(
                "could not transition tool call %s %s -> %s",
                call.tool_call_id,
                call.status.value,
                target.value,
            )


__all__ = [
    "RecoveryService",
    "RecoverySummary",
]
