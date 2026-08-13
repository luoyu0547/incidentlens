"""Bounded parent/container-child agent orchestrator for Phase 4.

``AgentOrchestrator`` runs one bounded ``AgentRun`` loop, shared by the parent
and every container child.  Each round follows the same safe sequence:

1. load the latest run + investigation state;
2. honour cancellation, terminal states and the guard's budget checks
   (investigation level first, then run level);
3. append the ``before_model_turn`` checkpoint;
4. call the provider and persist a structured round summary;
5. validate the provider's proposals with ``ProviderOutputValidator`` (which is
   handed the *full* ``AgentRun`` so budget/scope/ownership checks apply);
6. execute tools, delegate container children, or apply a stop signal;
7. fold new evidence and hypotheses into the run, update cumulative counters;
8. append the post-round checkpoint and decide to continue or stop safely.

A parent may dispatch several container children concurrently, bounded by the
investigation's child budget and a global semaphore.  Every child runs the same
loop with its own provider context, tool history, budget, container session,
allowed paths and reduced evidence package, and returns an evidence-grounded
``ChildReport`` (partial on cancel/crash/over-budget) plus an ``EvidenceReference``
to its report.  The parent only ever receives the structured report and evidence
refs -- never raw transcripts.  Closing a child's container session never touches
the host session.

The orchestrator writes state only through ``InvestigationStore``, so a run can
be resumed from its latest checkpoint after a restart.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.evidence.service import EvidenceService
from incidentlens_control_plane.investigation.events import InvestigationEventPublisher
from incidentlens_control_plane.investigation.guard import InvestigationGuard
from incidentlens_control_plane.investigation.provider import (
    AgentTurnRequest,
    AgentTurnResult,
    ChildDelegationRequest,
    InvestigationSnapshot,
    ModelProvider,
    ProviderContextMismatch,
    ProviderCrash,
    ProviderError,
    ProviderOutputRejected,
    ProviderOutputValidator,
    RunCheckpoint,
)
from incidentlens_control_plane.investigation.state_machine import (
    AGENT_RUN_STATE_MACHINE,
    INVESTIGATION_STATE_MACHINE,
    TOOL_CALL_STATE_MACHINE,
    AgentRunStatus,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.store import (
    AgentRunNotFound,
    AlreadyExists,
    CheckpointConflict,
    InvestigationStore,
    RoundConflict,
)
from incidentlens_control_plane.investigation.tool_executor import ToolExecutor
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    Checkpoint,
    ChildReport,
    ChildReportStatus,
    DelegatedTaskPackage,
    EvidenceReference,
    Hypothesis,
    HypothesisStatus,
    Investigation,
    StopReason,
    ToolCall,
    UsageCounters,
)
from incidentlens_control_plane.logs.redaction import redact_message
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import TargetRegistration
from incidentlens_control_plane.remote_ops.sessions import SessionManager


class RunNotFound(Exception):
    """Raised when a requested agent run does not exist."""


class _EvidenceBudgetExceeded(Exception):
    """Raised when attaching evidence would exceed the run/investigation cap."""


def _iso_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


class AgentOrchestrator:
    """Runs the bounded investigation loop for parents and container children."""

    def __init__(
        self,
        *,
        store: InvestigationStore,
        provider: ModelProvider,
        executor: ToolExecutor,
        evidence: EvidenceService,
        projects: ProjectRegistryStore,
        sessions: SessionManager,
        guard: InvestigationGuard | None = None,
        global_child_limit: int = 8,
        default_budget: AgentBudget | None = None,
        max_provider_retries: int = 2,
        now: Callable[[], datetime] | None = None,
        events: RuntimeEventStore | None = None,
        broker: RuntimeEventBroker | None = None,
    ) -> None:
        if global_child_limit < 1:
            raise ValueError("global_child_limit must be >= 1")
        self._store = store
        self._provider = provider
        self._executor = executor
        self._evidence = evidence
        self._projects = projects
        self._sessions = sessions
        self._guard = guard or InvestigationGuard()
        self._global_child_limit = global_child_limit
        self._default_budget = default_budget or AgentBudget()
        self._max_provider_retries = max_provider_retries
        self._now = now or (lambda: datetime.now(UTC))
        self._events_pub = (
            InvestigationEventPublisher(events, broker)
            if events is not None and broker is not None
            else None
        )
        # Bound concurrent children across all investigations sharing this
        # orchestrator.  The per-investigation cap is enforced by the guard's
        # ``can_spawn_child`` (``investigation.budget.max_children``).
        self._child_semaphore = asyncio.Semaphore(global_child_limit)
        # Every active parent/child loop task, tracked so the recovery service
        # can request an orderly drain on shutdown.  The store remains the
        # source of truth; this registry only lets shutdown cancel stragglers.
        self._active_loop_tasks: set[asyncio.Task] = set()

    # -- public entry points --------------------------------------------------

    async def run_investigation(
        self,
        investigation: Investigation,
        parent_scope: AgentScope,
        *,
        parent_budget: AgentBudget | None = None,
    ) -> AgentRun:
        """Create the parent run for *investigation* and run its bounded loop."""
        now = self._now()
        parent = self._build_parent_run(investigation, parent_scope, parent_budget, now)
        try:
            self._store.create_agent_run(parent)
        except AlreadyExists:
            parent = self._store.get_agent_run(parent.agent_run_id)
        return await self._run_loop(parent.agent_run_id)

    async def run(self, agent_run_id: str) -> AgentRun:
        """Run or resume the bounded loop for an existing agent run."""
        self._store.get_agent_run(agent_run_id)  # fail fast when unknown
        return await self._run_loop(agent_run_id)

    async def drain_active_loops(self, timeout: float) -> int:
        """Wait up to *timeout* for active loops, then cancel the stragglers.

        Shutdown path: after the recovery service parks every run as
        ``CANCEL_REQUESTED``, this gives the live loops a grace window to
        observe the request and finalise.  Loops still running after the grace
        window are cancelled so the process can exit; their runs are swept to a
        terminal state by the recovery service.
        """
        tasks = [task for task in list(self._active_loop_tasks) if not task.done()]
        if not tasks:
            return 0
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return len(tasks)

    async def cancel_active_loops(self) -> int:
        """Cancel every tracked active loop task and await it (shutdown path)."""
        tasks = [task for task in list(self._active_loop_tasks) if not task.done()]
        if not tasks:
            return 0
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    # -- the shared loop ------------------------------------------------------

    async def _run_loop(self, agent_run_id: str) -> AgentRun:
        task = asyncio.current_task()
        if task is not None:
            self._active_loop_tasks.add(task)
        try:
            return await self._run_loop_inner(agent_run_id)
        finally:
            if task is not None:
                self._active_loop_tasks.discard(task)

    async def _run_loop_inner(self, agent_run_id: str) -> AgentRun:
        pending_children: list[tuple[str, asyncio.Task]] = []
        child_reports: list[ChildReport] = []

        while True:
            try:
                outcome = await self._loop_step(agent_run_id, pending_children, child_reports)
            except _EvidenceBudgetExceeded as exc:
                # A child report could not be attached within the run's evidence
                # budget: pause safely instead of silently exceeding the cap.
                run = self._store.get_agent_run(agent_run_id)
                investigation = self._store.get_investigation(run.investigation_id)
                if run.status is AgentRunStatus.WAITING_CHILDREN:
                    # The drain that raised may have left the run in
                    # WAITING_CHILDREN, from which PAUSED_BUDGET is illegal.
                    run = self._transition_run(run, AgentRunStatus.RUNNING, now=self._now())
                    investigation = self._store.get_investigation(run.investigation_id)
                self._pause(
                    run, investigation, AgentRunStatus.PAUSED_BUDGET,
                    now=self._now(), reason=str(exc),
                    stop_reason=StopReason.BUDGET_EVIDENCE,
                )
                return self._store.get_agent_run(agent_run_id)
            if outcome is not None:
                return outcome

    async def _loop_step(
        self,
        agent_run_id: str,
        pending_children: list[tuple[str, asyncio.Task]],
        child_reports: list[ChildReport],
    ) -> AgentRun | None:
        """Run one bounded iteration; ``None`` means continue to the next round."""
        # Yield once per iteration so a concurrent cancellation lands between
        # rounds and background children make progress while the parent runs
        # its own rounds (the parent never blocks on them until it stops).
        await asyncio.sleep(0)

        run = self._store.get_agent_run(agent_run_id)
        investigation = self._store.get_investigation(run.investigation_id)
        now = self._now()

        # Drain child tasks that already finished (background concurrency).
        if pending_children:
            drained = self._drain_completed_children(
                run, investigation, pending_children, child_reports, now
            )
            if drained is not None:
                run, investigation = drained
                self._store.update_agent_run(run)
                self._store.update_investigation(investigation)
                run = self._store.get_agent_run(agent_run_id)
                investigation = self._store.get_investigation(run.investigation_id)

        # -- terminal / cancellation / waiting states --------------------
        if AGENT_RUN_STATE_MACHINE.is_terminal(run.status):
            if (
                run.kind is AgentRunKind.PARENT
                and run.status is AgentRunStatus.CANCELLED
                and investigation.status is InvestigationStatus.CANCEL_REQUESTED
            ):
                # A run cancelled before its loop ran (e.g. a CREATED run
                # parked directly) still owns the investigation final state.
                self._transition_investigation(
                    investigation, InvestigationStatus.CANCELLED,
                    now=now, stop_reason=StopReason.CANCELLED,
                )
            return run
        if INVESTIGATION_STATE_MACHINE.is_terminal(investigation.status):
            run, investigation = await self._drain_all_children(
                run, investigation, pending_children, child_reports, now
            )
            return self._sync_run_to_investigation(run, investigation, now)
        if run.status is AgentRunStatus.CANCEL_REQUESTED:
            run, investigation = await self._drain_all_children(
                run, investigation, pending_children, child_reports, now
            )
            self._transition_run(
                run, AgentRunStatus.CANCELLED, now=now, stop_reason=StopReason.CANCELLED
            )
            if investigation.status is InvestigationStatus.CANCEL_REQUESTED:
                self._transition_investigation(
                    investigation, InvestigationStatus.CANCELLED,
                    now=now, stop_reason=StopReason.CANCELLED,
                )
            return self._store.get_agent_run(agent_run_id)
        if run.status is AgentRunStatus.WAITING_APPROVAL:
            # Approval decisions are Task 8's responsibility; a run blocked
            # on approval stays paused until the decision resolves it.
            return run
        if run.status is AgentRunStatus.WAITING_CHILDREN:
            run, investigation = await self._resume_waiting_children(
                run, investigation, pending_children, child_reports, now
            )
            return None
        if run.status in {
            AgentRunStatus.PAUSED_BUDGET,
            AgentRunStatus.PAUSED_MISSING_EVIDENCE,
            AgentRunStatus.PAUSED_UNCERTAIN_STATE,
        }:
            # Resume re-evaluates the pause condition under current budgets.
            run = self._transition_run(run, AgentRunStatus.RUNNING, now=now)
            self._transition_investigation(
                investigation, InvestigationStatus.RUNNING, now=now
            )
            return None
        if run.status is AgentRunStatus.CREATED:
            run = self._transition_run(run, AgentRunStatus.RUNNING, now=now)
            return None

        # -- budget checks (investigation level, then run level) ----------
        ok, reason = self._guard.check_investigation_before_model_turn(
            investigation, now=now
        )
        if not ok:
            run, investigation = await self._drain_all_children(
                run, investigation, pending_children, child_reports, now
            )
            self._pause(run, investigation, self._pause_status_for(reason), now=now,
                        reason=reason, stop_reason=self._stop_reason_for(reason))
            return self._store.get_agent_run(agent_run_id)

        ok, reason = self._guard.check_before_model_turn(run, now=now)
        if not ok:
            run, investigation = await self._drain_all_children(
                run, investigation, pending_children, child_reports, now
            )
            self._pause(run, investigation, self._pause_status_for(reason), now=now,
                        reason=reason, stop_reason=self._stop_reason_for(reason))
            return self._store.get_agent_run(agent_run_id)

        # -- one bounded round -------------------------------------------
        round_number = run.usage.rounds + 1
        before_seq = 2 * round_number - 1
        post_seq = 2 * round_number
        self._write_checkpoint(run, sequence=before_seq, round_number=round_number, now=now)

        request = self._build_request(run, investigation, round_number, child_reports)
        try:
            result = await self._call_provider(request)
        except ProviderCrash as exc:
            run, investigation = await self._drain_all_children(
                run, investigation, pending_children, child_reports, now
            )
            return self._fail(run, investigation, now, reason=str(exc))
        except ProviderError as exc:
            run, investigation = await self._drain_all_children(
                run, investigation, pending_children, child_reports, now
            )
            return self._fail(run, investigation, now, reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - a misbehaving provider must not crash the loop
            run, investigation = await self._drain_all_children(
                run, investigation, pending_children, child_reports, now
            )
            return self._fail(run, investigation, now, reason=str(exc))

        run = self._bump_usage(run, rounds=run.usage.rounds + 1)
        self._store.update_agent_run(run)
        run = self._store.get_agent_run(agent_run_id)
        self._append_round_summary(run, round_number, result.usage, now)

        run = self._bump_usage(
            run,
            total_output_bytes=run.usage.total_output_bytes + result.usage.output_bytes,
        )
        self._store.update_agent_run(run)
        run = self._store.get_agent_run(agent_run_id)

        validator = ProviderOutputValidator(request, run)
        try:
            validator.validate(result)
        except (ProviderOutputRejected, ProviderContextMismatch) as exc:
            run, investigation = await self._drain_all_children(
                run, investigation, pending_children, child_reports, now
            )
            return self._fail(run, investigation, now, reason=str(exc))

        # Materialize and persist proposed hypotheses.
        for proposal in result.hypotheses:
            hypothesis = self._materialize_hypothesis(run, proposal, now)
            self._store.create_hypothesis(hypothesis)
            run = run.model_copy(
                update={"hypotheses": run.hypotheses + (hypothesis,)}
            )

        evidence_before = {ref.evidence_id for ref in run.evidence}

        # Act on the validated turn: stop, delegate, tools, or continue.
        if result.stop_signal is not None:
            run, investigation = await self._drain_all_children(
                run, investigation, pending_children, child_reports, now
            )
            run = self._handle_stop(
                run, investigation, result.stop_signal, result.conclusions, now
            )
            run, investigation = self._reload_pair(run)
            run, investigation = self._finish_round_counters(
                run, investigation, evidence_before, now
            )
            self._store.update_agent_run(run)
            self._store.update_investigation(investigation)
            self._write_checkpoint(run, sequence=post_seq, round_number=round_number, now=now)
            return self._store.get_agent_run(agent_run_id)

        if result.child_delegation is not None:
            run, stop = await self._delegate_child(
                run, investigation, result.child_delegation, pending_children, now
            )
            run, investigation = self._reload_pair(run)
            run, investigation = self._finish_round_counters(
                run, investigation, evidence_before, now
            )
            self._store.update_agent_run(run)
            self._store.update_investigation(investigation)
            self._write_checkpoint(run, sequence=post_seq, round_number=round_number, now=now)
            if stop:
                return self._store.get_agent_run(agent_run_id)
            return None

        if result.tool_requests:
            run, investigation, stop = await self._execute_tools(
                run, investigation, result.tool_requests, now
            )
            run, investigation = self._reload_pair(run)
            run, investigation = self._finish_round_counters(
                run, investigation, evidence_before, now
            )
            self._store.update_agent_run(run)
            self._store.update_investigation(investigation)
            self._write_checkpoint(run, sequence=post_seq, round_number=round_number, now=now)
            if stop:
                return self._store.get_agent_run(agent_run_id)
            # A ``delegate_child`` tool persisted a delegated-task package
            # without spawning its loop; launch those children now.
            self._spawn_delegated_packages(run, pending_children, now)
            return None

        # Hypotheses/conclusions with no stop, delegation or tool: an
        # ungrounded conclusion is a missing-evidence signal; otherwise fold
        # and continue (the no-new-evidence counter catches a stalled run).
        ungrounded = any(not conclusion.evidence_ids for conclusion in result.conclusions)
        for conclusion in result.conclusions:
            self._persist_conclusion(run, conclusion, now)
        if ungrounded:
            run, investigation = self._finish_round_counters(
                run, investigation, evidence_before, now
            )
            self._store.update_agent_run(run)
            self._store.update_investigation(investigation)
            self._write_checkpoint(run, sequence=post_seq, round_number=round_number, now=now)
            self._pause(
                run, investigation, AgentRunStatus.PAUSED_MISSING_EVIDENCE,
                now=now, reason="conclusion cites no evidence",
                stop_reason=StopReason.MISSING_EVIDENCE,
            )
            return self._store.get_agent_run(agent_run_id)
        run, investigation = self._finish_round_counters(
            run, investigation, evidence_before, now
        )
        self._store.update_agent_run(run)
        self._store.update_investigation(investigation)
        self._write_checkpoint(run, sequence=post_seq, round_number=round_number, now=now)

        if (
            run.usage.consecutive_no_new_evidence_rounds
            >= run.budget.max_no_new_evidence_rounds
        ):
            self._pause(
                run, investigation, AgentRunStatus.PAUSED_MISSING_EVIDENCE,
                now=now, reason="no-new-evidence budget exhausted",
                stop_reason=StopReason.BUDGET_NO_NEW_EVIDENCE,
            )
            return self._store.get_agent_run(agent_run_id)

        return None

    # -- provider -------------------------------------------------------------

    async def _call_provider(self, request: AgentTurnRequest) -> AgentTurnResult:
        retries = 0
        while True:
            try:
                return await self._provider.generate_turn(request)
            except ProviderError as exc:
                if not exc.retryable or retries >= self._max_provider_retries:
                    raise
                retries += 1
                run = self._store.get_agent_run(request.checkpoint.agent_run_id)
                if (
                    AGENT_RUN_STATE_MACHINE.is_terminal(run.status)
                    or run.status is AgentRunStatus.CANCEL_REQUESTED
                ):
                    raise exc

    def _build_request(
        self,
        run: AgentRun,
        investigation: Investigation,
        round_number: int,
        child_reports: list[ChildReport],
    ) -> AgentTurnRequest:
        return AgentTurnRequest(
            checkpoint=RunCheckpoint(
                agent_run_id=run.agent_run_id,
                kind=run.kind,
                status=run.status,
                round_number=round_number,
                parent_run_id=run.parent_run_id,
                scope=run.scope,
                budget=run.budget,
                usage=run.usage,
            ),
            investigation=InvestigationSnapshot(
                investigation_id=investigation.investigation_id,
                incident_id=investigation.incident_id,
                symptom=investigation.symptom,
                status=investigation.status,
                budget=investigation.budget,
                usage=investigation.usage,
            ),
            hypotheses=run.hypotheses[-64:],
            evidence=run.evidence[-100:],
            child_reports=tuple(child_reports)[-8:],
            tool_schemas=self._executor.tool_schemas(scope=run.scope.scope),
        )

    # -- stop / pause / fail --------------------------------------------------

    def _handle_stop(
        self,
        run: AgentRun,
        investigation: Investigation,
        stop_signal: Any,
        conclusions: tuple[Any, ...],
        now: datetime,
    ) -> AgentRun:
        reason = stop_signal.stop_reason
        if reason is StopReason.COMPLETED:
            # Completion must be grounded: at least one conclusion citing run
            # evidence.  A COMPLETED stop with no conclusions (or with an
            # ungrounded one) is a missing-evidence signal, never a fabrication.
            if not conclusions or any(
                not conclusion.evidence_ids for conclusion in conclusions
            ):
                self._pause(
                    run, investigation, AgentRunStatus.PAUSED_MISSING_EVIDENCE,
                    now=now, reason="completion requires an evidence-grounded conclusion",
                    stop_reason=StopReason.MISSING_EVIDENCE,
                )
                return self._store.get_agent_run(run.agent_run_id)
            for conclusion in conclusions:
                self._persist_conclusion(run, conclusion, now)
            self._transition_run(
                run, AgentRunStatus.COMPLETED, now=now, stop_reason=StopReason.COMPLETED
            )
            if run.kind is AgentRunKind.PARENT:
                self._transition_investigation(
                    investigation, InvestigationStatus.COMPLETED, now=now,
                    stop_reason=StopReason.COMPLETED,
                )
            return self._store.get_agent_run(run.agent_run_id)
        if reason is StopReason.MISSING_EVIDENCE:
            self._pause(run, investigation, AgentRunStatus.PAUSED_MISSING_EVIDENCE, now=now,
                        reason=stop_signal.summary, stop_reason=StopReason.MISSING_EVIDENCE)
            return self._store.get_agent_run(run.agent_run_id)
        if reason is StopReason.PENDING_APPROVAL:
            self._pause(run, investigation, AgentRunStatus.WAITING_APPROVAL, now=now,
                        reason=stop_signal.summary, stop_reason=StopReason.PENDING_APPROVAL)
            return self._store.get_agent_run(run.agent_run_id)
        if reason is StopReason.UNCERTAIN_STATE:
            self._pause(run, investigation, AgentRunStatus.PAUSED_UNCERTAIN_STATE, now=now,
                        reason=stop_signal.summary, stop_reason=StopReason.UNCERTAIN_STATE)
            return self._store.get_agent_run(run.agent_run_id)
        if reason is StopReason.CANCELLED:
            # A provider-declared cancellation finalises the run to CANCELLED
            # (RUNNING -> CANCELLED is not a legal move, so go via CANCEL_REQUESTED).
            self._transition_run(run, AgentRunStatus.CANCEL_REQUESTED, now=now,
                                 stop_reason=StopReason.CANCELLED)
            self._transition_run(
                self._store.get_agent_run(run.agent_run_id),
                AgentRunStatus.CANCELLED, now=now, stop_reason=StopReason.CANCELLED,
            )
            if run.kind is AgentRunKind.PARENT:
                self._transition_investigation(investigation, InvestigationStatus.CANCEL_REQUESTED,
                                               now=now, stop_reason=StopReason.CANCELLED)
                self._transition_investigation(
                    self._store.get_investigation(investigation.investigation_id),
                    InvestigationStatus.CANCELLED, now=now, stop_reason=StopReason.CANCELLED,
                )
            return self._store.get_agent_run(run.agent_run_id)
        if reason is StopReason.FAILED:
            return self._fail(run, investigation, now, reason=stop_signal.summary)
        return self._fail(run, investigation, now, reason=f"unhandled stop reason {reason.value}")

    def _finish_round_counters(
        self,
        run: AgentRun,
        investigation: Investigation,
        evidence_before: set[str],
        now: datetime,
    ) -> tuple[AgentRun, Investigation]:
        # Reload the investigation fresh before writing: a concurrent child may
        # have bumped usage during this round's provider/tool awaits, and a
        # stale copy would clobber those increments (rounds / no-new-evidence
        # budgets would be silently weakened).
        investigation = self._store.get_investigation(investigation.investigation_id)
        run = self._bump_usage(run, wall_clock_seconds=self._elapsed(run, now))
        investigation = self._bump_investigation_usage(
            investigation, rounds=investigation.usage.rounds + 1
        )
        evidence_added = bool({ref.evidence_id for ref in run.evidence} - evidence_before)
        if evidence_added:
            run = self._bump_usage(run, consecutive_no_new_evidence_rounds=0)
            investigation = self._bump_investigation_usage(
                investigation, consecutive_no_new_evidence_rounds=0
            )
        else:
            run = self._bump_usage(
                run,
                consecutive_no_new_evidence_rounds=(
                    run.usage.consecutive_no_new_evidence_rounds + 1
                ),
            )
            investigation = self._bump_investigation_usage(
                investigation,
                consecutive_no_new_evidence_rounds=(
                    investigation.usage.consecutive_no_new_evidence_rounds + 1
                ),
            )
        return run, investigation

    def _fail(
        self, run: AgentRun, investigation: Investigation, now: datetime, *, reason: str
    ) -> AgentRun:
        self._transition_run(
            run, AgentRunStatus.FAILED, now=now, stop_reason=StopReason.FAILED
        )
        if run.kind is AgentRunKind.PARENT:
            self._transition_investigation(
                investigation, InvestigationStatus.FAILED, now=now,
                stop_reason=StopReason.FAILED,
            )
        return self._store.get_agent_run(run.agent_run_id)

    def _pause(
        self,
        run: AgentRun,
        investigation: Investigation,
        run_status: AgentRunStatus,
        *,
        now: datetime,
        reason: str,
        stop_reason: StopReason,
    ) -> None:
        self._transition_run(run, run_status, now=now, stop_reason=stop_reason)
        if run.kind is AgentRunKind.PARENT:
            self._transition_investigation(
                investigation, self._investigation_pause(run_status), now=now,
                stop_reason=stop_reason,
            )

    @staticmethod
    def _investigation_pause(run_status: AgentRunStatus) -> InvestigationStatus:
        if run_status is AgentRunStatus.WAITING_APPROVAL:
            return InvestigationStatus.WAITING_APPROVAL
        if run_status is AgentRunStatus.PAUSED_MISSING_EVIDENCE:
            return InvestigationStatus.PAUSED_MISSING_EVIDENCE
        if run_status is AgentRunStatus.PAUSED_UNCERTAIN_STATE:
            return InvestigationStatus.PAUSED_UNCERTAIN_STATE
        return InvestigationStatus.PAUSED_BUDGET

    @staticmethod
    def _pause_status_for(reason: str) -> AgentRunStatus:
        if "no-new-evidence" in reason or "no new evidence" in reason:
            return AgentRunStatus.PAUSED_MISSING_EVIDENCE
        return AgentRunStatus.PAUSED_BUDGET

    @staticmethod
    def _stop_reason_for(reason: str) -> StopReason:
        if "round" in reason:
            return StopReason.BUDGET_ROUNDS
        if "tool-call" in reason:
            return StopReason.BUDGET_TOOL_CALLS
        if "wall-clock" in reason or "wall clock" in reason:
            return StopReason.BUDGET_TIME
        if "no-new-evidence" in reason or "no new evidence" in reason:
            return StopReason.BUDGET_NO_NEW_EVIDENCE
        if "output" in reason:
            return StopReason.BUDGET_OUTPUT
        if "evidence budget" in reason:
            return StopReason.BUDGET_EVIDENCE
        if "child budget" in reason:
            return StopReason.BUDGET_CHILDREN
        return StopReason.BUDGET_ROUNDS

    # -- tool execution -------------------------------------------------------

    async def _execute_tools(
        self,
        run: AgentRun,
        investigation: Investigation,
        tool_requests: tuple[Any, ...],
        now: datetime,
    ) -> tuple[AgentRun, Investigation, bool]:
        for request in tool_requests:
            # I3: reload fresh before each tool so a concurrent child's
            # investigation-usage increments are never clobbered by a stale copy.
            run = self._store.get_agent_run(run.agent_run_id)
            investigation = self._store.get_investigation(run.investigation_id)

            ok, reason = self._guard.check_before_tool_execution(run, now=now)
            if not ok:
                self._pause(
                    run, investigation, self._pause_status_for(reason), now=now,
                    reason=reason, stop_reason=self._stop_reason_for(reason),
                )
                return self._store.get_agent_run(run.agent_run_id), investigation, True

            ok, reason = self._guard.check_investigation_before_tool_execution(
                investigation, now=now
            )
            if not ok:
                self._pause(
                    run, investigation, self._pause_status_for(reason), now=now,
                    reason=reason, stop_reason=self._stop_reason_for(reason),
                )
                return self._store.get_agent_run(run.agent_run_id), investigation, True

            tool_call = self._load_or_create_tool_call(run, request, now)
            if TOOL_CALL_STATE_MACHINE.is_terminal(tool_call.status):
                continue
            # C1: persist the execution before the executor runs, so a crash
            # mid-tool leaves a RUNNING call that startup recovery can
            # classify (a dangerous call is parked UNCERTAIN, never replayed).
            # A pre-existing RUNNING call is a resume re-entry, left untouched.
            if tool_call.status is not ToolCallStatus.RUNNING:
                tool_call = self._transition_tool_call(
                    tool_call, ToolCallStatus.RUNNING, now=now
                )

            outcome = await self._executor.execute(request, run, now=now)

            # I3: reload fresh after the tool await so a concurrent child's
            # investigation-usage increments are never clobbered by a stale copy.
            run = self._store.get_agent_run(run.agent_run_id)
            investigation = self._store.get_investigation(run.investigation_id)

            if outcome.status is ToolCallStatus.WAITING_APPROVAL:
                # The call is already RUNNING (stamped before the executor ran);
                # the executor decided approval dynamically, so the call parks
                # on WAITING_APPROVAL until the decision lands.
                self._transition_tool_call(
                    tool_call, ToolCallStatus.WAITING_APPROVAL, now=now,
                    approval_id=outcome.approval_id,
                )
                self._store.update_agent_run(run)
                self._store.update_investigation(investigation)
                self._pause(
                    run, investigation, AgentRunStatus.WAITING_APPROVAL, now=now,
                    reason="tool requires approval", stop_reason=StopReason.PENDING_APPROVAL,
                )
                return self._store.get_agent_run(run.agent_run_id), investigation, True

            # I2: cumulative output budgets (run + investigation) before counting.
            ok, reason = self._guard.can_accept_output(run, outcome.output_bytes)
            if not ok:
                self._transition_tool_call(
                    tool_call, outcome.status, now=now,
                    output_bytes=outcome.output_bytes,
                    evidence_ids=tuple(ref.evidence_id for ref in outcome.evidence),
                    error_redacted=outcome.error_redacted,
                )
                self._pause(
                    run, investigation, AgentRunStatus.PAUSED_BUDGET,
                    now=now, reason=reason, stop_reason=StopReason.BUDGET_OUTPUT,
                )
                return self._store.get_agent_run(run.agent_run_id), investigation, True
            ok, reason = self._guard.can_investigation_accept_output(
                investigation, outcome.output_bytes
            )
            if not ok:
                self._transition_tool_call(
                    tool_call, outcome.status, now=now,
                    output_bytes=outcome.output_bytes,
                    evidence_ids=tuple(ref.evidence_id for ref in outcome.evidence),
                    error_redacted=outcome.error_redacted,
                )
                self._pause(
                    run, investigation, AgentRunStatus.PAUSED_BUDGET,
                    now=now, reason=reason, stop_reason=StopReason.BUDGET_OUTPUT,
                )
                return self._store.get_agent_run(run.agent_run_id), investigation, True

            self._transition_tool_call(
                tool_call, outcome.status, now=now,
                output_bytes=outcome.output_bytes,
                evidence_ids=tuple(ref.evidence_id for ref in outcome.evidence),
                error_redacted=outcome.error_redacted,
            )
            # I1: enforce the evidence budget (run + investigation) when attaching.
            try:
                run, new_evidence = self._append_evidence(
                    run, investigation, outcome.evidence, now
                )
            except _EvidenceBudgetExceeded:
                self._pause(
                    run, investigation, AgentRunStatus.PAUSED_BUDGET,
                    now=now, reason="evidence budget exhausted",
                    stop_reason=StopReason.BUDGET_EVIDENCE,
                )
                return self._store.get_agent_run(run.agent_run_id), investigation, True
            run = self._bump_usage(
                run,
                tool_calls=run.usage.tool_calls + 1,
                total_output_bytes=run.usage.total_output_bytes + outcome.output_bytes,
            )
            investigation = self._bump_investigation_usage(
                investigation,
                tool_calls=investigation.usage.tool_calls + 1,
                total_output_bytes=investigation.usage.total_output_bytes + outcome.output_bytes,
                evidence_count=investigation.usage.evidence_count + new_evidence,
            )
            self._store.update_agent_run(run)
            self._store.update_investigation(investigation)
            run = self._store.get_agent_run(run.agent_run_id)
            investigation = self._store.get_investigation(investigation.investigation_id)

            if outcome.status is ToolCallStatus.UNCERTAIN:
                self._pause(run, investigation, AgentRunStatus.PAUSED_UNCERTAIN_STATE, now=now,
                            reason=outcome.summary, stop_reason=StopReason.UNCERTAIN_STATE)
                return self._store.get_agent_run(run.agent_run_id), investigation, True

        return run, investigation, False

    def _load_or_create_tool_call(self, run: AgentRun, request: Any, now: datetime) -> ToolCall:
        try:
            return self._store.get_tool_call(request.tool_call_id)
        except Exception:
            pass
        tool_call = ToolCall(
            tool_call_id=request.tool_call_id,
            agent_run_id=run.agent_run_id,
            tool_name=request.tool_name,
            status=ToolCallStatus.PLANNED,
            idempotency_key=request.tool_call_id,
            planned_at=now,
            arguments=request.arguments,
        )
        try:
            return self._store.create_tool_call(tool_call)
        except AlreadyExists:
            return self._store.get_tool_call(request.tool_call_id)

    def _transition_tool_call(
        self,
        tool_call: ToolCall,
        target: ToolCallStatus,
        *,
        now: datetime,
        output_bytes: int | None = None,
        evidence_ids: tuple[str, ...] | None = None,
        error_redacted: str | None = None,
        approval_id: str | None = None,
    ) -> ToolCall:
        previous = tool_call.status.value
        updated = self._store.transition_tool_call_status(
            tool_call.tool_call_id,
            target,
            now=now,
            output_bytes=output_bytes,
            evidence_ids=evidence_ids,
            error_redacted=error_redacted,
            approval_id=approval_id,
        )
        if self._events_pub is not None:
            if previous != updated.status.value:
                self._events_pub.tool_call_status_changed(
                    updated, previous=previous, occurred_at=now
                )
            if (
                previous == ToolCallStatus.PLANNED.value
                and updated.status
                in (ToolCallStatus.RUNNING, ToolCallStatus.WAITING_APPROVAL)
            ):
                self._events_pub.tool_call_started(updated, occurred_at=now)
            if TOOL_CALL_STATE_MACHINE.is_terminal(updated.status):
                self._events_pub.tool_call_completed(updated, occurred_at=now)
        return updated

    # -- child delegation -----------------------------------------------------

    async def _delegate_child(
        self,
        run: AgentRun,
        investigation: Investigation,
        delegation: ChildDelegationRequest,
        pending_children: list[tuple[str, asyncio.Task]],
        now: datetime,
    ) -> tuple[AgentRun, bool]:
        allowed, reason = self._guard.can_spawn_child(run, investigation)
        if not allowed:
            if run.kind is AgentRunKind.CHILD:
                # A child must never delegate grandchildren.
                return self._fail(run, investigation, now, reason=reason), True
            self._pause(run, investigation, AgentRunStatus.PAUSED_BUDGET, now=now,
                        reason=reason, stop_reason=StopReason.BUDGET_CHILDREN)
            return self._store.get_agent_run(run.agent_run_id), True

        budget = self._child_budget(delegation, run)
        package = DelegatedTaskPackage(
            child_run_id=delegation.child_run_id,
            parent_run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            task_prompt=delegation.task_prompt,
            scope=delegation.scope,
            budget=budget,
            evidence_ids=delegation.evidence_ids,
        )
        try:
            self._store.create_delegated_task(package, now=now)
        except AlreadyExists:
            pass

        investigation = self._store.get_investigation(run.investigation_id)
        investigation = self._bump_investigation_usage(
            investigation, children=investigation.usage.children + 1
        )
        self._store.update_investigation(investigation)

        self._spawn_child_from_package(package, run, pending_children, now)
        # The parent stays RUNNING so it can keep delegating; children run
        # concurrently in the background until the parent stops.
        return run, False

    def _spawn_child_from_package(
        self,
        package: DelegatedTaskPackage,
        parent_run: AgentRun,
        pending_children: list[tuple[str, asyncio.Task]],
        now: datetime,
    ) -> None:
        child = self._build_child_run(package, parent_run, now)
        try:
            self._store.create_agent_run(child)
        except AlreadyExists:
            child = self._store.get_agent_run(package.child_run_id)
        if any(child_id == child.agent_run_id for child_id, _ in pending_children):
            return
        task = asyncio.create_task(self._run_child(package))
        pending_children.append((child.agent_run_id, task))

    def _spawn_delegated_packages(
        self,
        run: AgentRun,
        pending_children: list[tuple[str, asyncio.Task]],
        now: datetime,
    ) -> None:
        """Spawn child loops for delegated-task packages lacking a child run.

        The provider-proposal path creates the child run up front, so those
        packages are skipped; the ``delegate_child`` tool path only persists the
        package, so its child loop is launched here.
        """
        pending_ids = {child_id for child_id, _ in pending_children}
        for package in self._store.list_delegated_tasks(parent_run_id=run.agent_run_id):
            if package.child_run_id in pending_ids:
                continue
            try:
                existing = self._store.get_agent_run(package.child_run_id)
            except AgentRunNotFound:
                existing = None
            if existing is not None and not AGENT_RUN_STATE_MACHINE.is_terminal(existing.status):
                continue
            if existing is not None:
                continue
            self._spawn_child_from_package(package, run, pending_children, now)

    def _build_child_run(
        self,
        package: DelegatedTaskPackage,
        parent_run: AgentRun,
        now: datetime,
    ) -> AgentRun:
        seed = {
            ref.evidence_id
            for ref in parent_run.evidence
            if ref.evidence_id in set(package.evidence_ids)
        }
        seed_evidence = tuple(
            ref for ref in parent_run.evidence if ref.evidence_id in seed
        )
        return AgentRun(
            agent_run_id=package.child_run_id,
            investigation_id=package.investigation_id,
            parent_run_id=package.parent_run_id,
            kind=AgentRunKind.CHILD,
            scope=package.scope,
            status=AgentRunStatus.CREATED,
            budget=package.budget,
            usage=UsageCounters(),
            evidence=seed_evidence,
            created_at=now,
            updated_at=now,
        )

    def _child_budget(
        self, delegation: ChildDelegationRequest, parent: AgentRun
    ) -> AgentBudget:
        # The provider's delegation carries no budget: default it, then cap every
        # axis by the parent's budget so a child can never widen its envelope.
        default = AgentBudget()
        values = {
            name: getattr(default, name)
            for name in AgentBudget.model_fields
        }
        for name in AgentBudget.model_fields:
            if getattr(default, name) > getattr(parent.budget, name):
                values[name] = getattr(parent.budget, name)
        return AgentBudget(**values)

    async def _run_child(
        self, package: DelegatedTaskPackage
    ) -> tuple[ChildReport, EvidenceReference]:
        child_id = package.child_run_id
        container_session_id: str | None = None
        try:
            async with self._child_semaphore:
                run = self._store.get_agent_run(child_id)
                if self._events_pub is not None:
                    self._events_pub.child_run_started(run, occurred_at=self._now())
                if run.scope.scope is LogScope.CONTAINER:
                    container_session_id = await self._spawn_container_session(run)
                final = await self._run_loop(child_id)
            if self._events_pub is not None:
                self._events_pub.child_run_completed(final, occurred_at=self._now())
            return self._build_child_report(final, package, now=self._now())
        except asyncio.CancelledError:
            run = self._store.get_agent_run(child_id)
            self._finalize_child_terminal(run, AgentRunStatus.CANCELLED, StopReason.CANCELLED)
            final = self._store.get_agent_run(child_id)
            if self._events_pub is not None:
                self._events_pub.child_run_completed(final, occurred_at=self._now())
            return self._build_child_report(
                final, package, now=self._now()
            )
        except Exception as exc:
            run = self._store.get_agent_run(child_id)
            self._finalize_child_terminal(
                run, AgentRunStatus.FAILED, StopReason.FAILED
            )
            final = self._store.get_agent_run(child_id)
            if self._events_pub is not None:
                self._events_pub.child_run_completed(final, occurred_at=self._now())
            return self._build_child_report(
                final,
                package,
                now=self._now(),
                error=str(exc),
            )
        finally:
            if container_session_id is not None:
                await self._sessions.close_container_session(container_session_id)

    def _finalize_child_terminal(
        self, run: AgentRun, target: AgentRunStatus, stop_reason: StopReason
    ) -> None:
        if AGENT_RUN_STATE_MACHINE.is_terminal(run.status):
            return
        if run.status is AgentRunStatus.CANCEL_REQUESTED:
            if target is AgentRunStatus.CANCELLED:
                self._transition_run(
                    run, AgentRunStatus.CANCELLED, now=self._now(), stop_reason=stop_reason
                )
            return
        self._transition_run(run, target, now=self._now(), stop_reason=stop_reason)

    async def _spawn_container_session(self, run: AgentRun) -> str:
        target = self._resolve_target(run)
        host = await self._sessions.connect(target)
        child = await self._sessions.spawn_container_session(
            host.session_id, run.scope.container_name
        )
        return child.session_id

    def _resolve_target(self, run: AgentRun) -> TargetRegistration:
        project = self._projects.get(run.scope.project_id)
        for target in project.targets:
            if target.target_id == run.scope.target_id:
                return target
        raise RunNotFound(
            f"target {run.scope.target_id!r} is not registered for "
            f"project {run.scope.project_id!r}"
        )

    # -- child report plumbing ------------------------------------------------

    def _build_child_report(
        self,
        child_run: AgentRun,
        package: DelegatedTaskPackage,
        *,
        now: datetime,
        error: str | None = None,
    ) -> tuple[ChildReport, EvidenceReference]:
        complete = child_run.status is AgentRunStatus.COMPLETED
        stop_reason = child_run.stop_reason or (
            StopReason.COMPLETED if complete else StopReason.FAILED
        )
        if complete:
            status = ChildReportStatus.COMPLETE
            conclusions = self._store.list_conclusions(agent_run_id=child_run.agent_run_id)
            parts = [f"child {child_run.agent_run_id} completed: {stop_reason.value}"]
            if conclusions:
                parts.append(conclusions[-1].summary)
            summary = "; ".join(parts)[:4_000]
        else:
            status = ChildReportStatus.PARTIAL
            parts = [f"child {child_run.agent_run_id} stopped with {stop_reason.value}"]
            if error is not None:
                parts.append(redact_message(error, max_length=1_000).message_redacted)
            if child_run.usage.tool_calls:
                parts.append(f"{child_run.usage.tool_calls} tool call(s) executed")
            summary = "; ".join(parts)[:4_000]

        report = ChildReport(
            agent_run_id=child_run.agent_run_id,
            parent_run_id=package.parent_run_id,
            status=status,
            summary=summary,
            findings=tuple(ref.summary[:600] for ref in child_run.evidence[:10]),
            inferences=(),
            unknowns=(),
            limitations=(),
            evidence_ids=tuple(ref.evidence_id for ref in child_run.evidence)[:24],
            stop_reason=stop_reason,
            created_at=now,
        )
        investigation = self._store.get_investigation(child_run.investigation_id)
        service_name = child_run.scope.service_name or "host"
        recorded = self._evidence.record_child_report(
            agent_run_id=package.parent_run_id,
            incident_id=investigation.incident_id,
            project_id=child_run.scope.project_id,
            target_id=child_run.scope.target_id,
            service_name=service_name,
            source_ref=f"child:{child_run.agent_run_id}",
            report_summary=report.summary,
            child_run_id=child_run.agent_run_id,
            parent_run_id=package.parent_run_id,
            status=status.value,
            stop_reason=stop_reason.value,
            created_by="agent",
            now=now,
        )
        evidence_ref = EvidenceReference(
            evidence_id=recorded.evidence_ref_id,
            operation_id=f"child:{child_run.agent_run_id}",
            summary=report.summary[:2_000],
        )
        return report, evidence_ref

    def _drain_completed_children(
        self,
        run: AgentRun,
        investigation: Investigation,
        pending_children: list[tuple[str, asyncio.Task]],
        child_reports: list[ChildReport],
        now: datetime,
    ) -> tuple[AgentRun, Investigation] | None:
        remaining: list[tuple[str, asyncio.Task]] = []
        changed = False
        try:
            for child_id, task in pending_children:
                if not task.done():
                    remaining.append((child_id, task))
                    continue
                changed = True
                try:
                    report, ref = task.result()
                except BaseException:
                    continue
                run, new_evidence = self._append_evidence(
                    run, investigation, (ref,), now
                )
                if new_evidence:
                    investigation = self._bump_investigation_usage(
                        investigation,
                        evidence_count=investigation.usage.evidence_count + new_evidence,
                    )
                child_reports.append(report)
        except _EvidenceBudgetExceeded:
            # Persist the already-drained child reports and restore RUNNING so
            # the loop's evidence-budget pause can transition legally (a
            # WAITING_CHILDREN run cannot move straight to PAUSED_BUDGET).
            pending_children[:] = remaining
            self._store.update_agent_run(run)
            self._store.update_investigation(investigation)
            if run.status is AgentRunStatus.WAITING_CHILDREN:
                self._transition_run(run, AgentRunStatus.RUNNING, now=now)
            raise
        pending_children[:] = remaining
        return (run, investigation) if changed else None

    async def _drain_all_children(
        self,
        run: AgentRun,
        investigation: Investigation,
        pending_children: list[tuple[str, asyncio.Task]],
        child_reports: list[ChildReport],
        now: datetime,
    ) -> tuple[AgentRun, Investigation]:
        if not pending_children:
            return run, investigation
        was_running = run.status is AgentRunStatus.RUNNING
        if was_running:
            run = self._transition_run(run, AgentRunStatus.WAITING_CHILDREN, now=now)
        tasks = [task for _, task in pending_children]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        pending_children.clear()
        # I3: reload fresh after the gather so a concurrent child's
        # investigation-usage increments are never clobbered by a stale copy.
        run = self._store.get_agent_run(run.agent_run_id)
        investigation = self._store.get_investigation(run.investigation_id)
        try:
            for outcome in results:
                if isinstance(outcome, BaseException):
                    continue
                report, ref = outcome
                run, new_evidence = self._append_evidence(
                    run, investigation, (ref,), now
                )
                if new_evidence:
                    investigation = self._bump_investigation_usage(
                        investigation,
                        evidence_count=investigation.usage.evidence_count + new_evidence,
                    )
                child_reports.append(report)
        except _EvidenceBudgetExceeded:
            # Persist the already-drained child reports and restore RUNNING so
            # the loop's evidence-budget pause can transition legally (a
            # WAITING_CHILDREN run cannot move straight to PAUSED_BUDGET).
            self._store.update_agent_run(run)
            self._store.update_investigation(investigation)
            if was_running:
                run = self._transition_run(run, AgentRunStatus.RUNNING, now=now)
            raise
        # Persist the appended child-report evidence before the next status
        # transition (which would otherwise discard the in-memory append).
        self._store.update_agent_run(run)
        self._store.update_investigation(investigation)
        if was_running:
            run = self._transition_run(run, AgentRunStatus.RUNNING, now=now)
        return self._store.get_agent_run(run.agent_run_id), investigation

    async def _resume_waiting_children(
        self,
        run: AgentRun,
        investigation: Investigation,
        pending_children: list[tuple[str, asyncio.Task]],
        child_reports: list[ChildReport],
        now: datetime,
    ) -> tuple[AgentRun, Investigation]:
        # Re-discover child runs that were in flight when the process died and
        # re-spawn their loops so a WAITING_CHILDREN parent can make progress.
        if pending_children:
            unfinished = [task for _, task in pending_children if not task.done()]
            if unfinished:
                await asyncio.wait(unfinished, return_when=asyncio.FIRST_COMPLETED)
            return run, investigation
        for child in self._store.list_agent_runs(parent_run_id=run.agent_run_id):
            if AGENT_RUN_STATE_MACHINE.is_terminal(child.status):
                continue
            try:
                package = self._store.get_delegated_task(child.agent_run_id)
            except Exception:
                continue
            self._spawn_child_from_package(package, run, pending_children, now)
        if not pending_children:
            run = self._transition_run(run, AgentRunStatus.RUNNING, now=now)
            return run, investigation
        return run, investigation

    def _sync_run_to_investigation(
        self, run: AgentRun, investigation: Investigation, now: datetime
    ) -> AgentRun:
        if AGENT_RUN_STATE_MACHINE.is_terminal(run.status):
            return run
        if investigation.status is InvestigationStatus.COMPLETED:
            self._transition_run(
                run, AgentRunStatus.COMPLETED, now=now, stop_reason=StopReason.COMPLETED
            )
        elif investigation.status is InvestigationStatus.CANCELLED:
            if run.status is AgentRunStatus.CANCEL_REQUESTED:
                self._transition_run(
                    run, AgentRunStatus.CANCELLED, now=now, stop_reason=StopReason.CANCELLED
                )
            else:
                self._transition_run(
                    run, AgentRunStatus.CANCEL_REQUESTED, now=now,
                    stop_reason=StopReason.CANCELLED,
                )
                self._transition_run(
                    self._store.get_agent_run(run.agent_run_id),
                    AgentRunStatus.CANCELLED, now=now, stop_reason=StopReason.CANCELLED,
                )
        else:
            self._transition_run(
                run, AgentRunStatus.FAILED, now=now, stop_reason=StopReason.FAILED
            )
        return self._store.get_agent_run(run.agent_run_id)

    # -- persistence helpers --------------------------------------------------

    def _build_parent_run(
        self,
        investigation: Investigation,
        parent_scope: AgentScope,
        parent_budget: AgentBudget | None,
        now: datetime,
    ) -> AgentRun:
        return AgentRun(
            agent_run_id=f"run-{uuid.uuid4().hex[:16]}",
            investigation_id=investigation.investigation_id,
            parent_run_id=None,
            kind=AgentRunKind.PARENT,
            scope=parent_scope,
            status=AgentRunStatus.CREATED,
            budget=parent_budget or self._default_budget,
            usage=UsageCounters(),
            created_at=now,
            updated_at=now,
        )

    def _materialize_hypothesis(self, run: AgentRun, proposal: Any, now: datetime) -> Hypothesis:
        return Hypothesis(
            hypothesis_id=f"hyp-{uuid.uuid4().hex[:16]}",
            agent_run_id=run.agent_run_id,
            summary=proposal.summary,
            facts=proposal.facts,
            inferences=proposal.inferences,
            unknowns=proposal.unknowns,
            evidence_ids=proposal.evidence_ids,
            status=HypothesisStatus.PROPOSED,
            created_at=now,
            updated_at=now,
        )

    def _persist_conclusion(self, run: AgentRun, conclusion: Any, now: datetime) -> None:
        # Conclusions that cite evidence are persisted; empty citations signal a
        # missing-evidence stop and are surfaced by the pause logic instead.
        if not conclusion.evidence_ids:
            return
        self._store.create_conclusion(run.agent_run_id, run.investigation_id, conclusion, now=now)

    def _append_evidence(
        self,
        run: AgentRun,
        investigation: Investigation,
        refs: tuple[EvidenceReference, ...],
        now: datetime,
    ) -> tuple[AgentRun, int]:
        """Attach new evidence refs, enforcing run + investigation budgets.

        Returns ``(run, new_count)``.  Raises ``_EvidenceBudgetExceeded`` when
        attaching would push either the run or the investigation over its
        ``max_evidence`` cap, so the caller can pause safely.  The
        investigation's cumulative ``evidence_count`` is *not* bumped here --
        callers merge ``new_count`` into a freshly reloaded investigation so a
        concurrent child's increments are never clobbered (last-writer-wins).
        """
        known = {ref.evidence_id for ref in run.evidence}
        new_refs = tuple(ref for ref in refs if ref.evidence_id not in known)
        if not new_refs:
            return run, 0
        # The guard's can_accept_*_evidence methods are the canonical budget
        # gates; the incremental len(new_refs) check catches a single large
        # tool result that would overshoot the cap in one shot.
        allowed, reason = self._guard.can_accept_new_evidence(run)
        if not allowed:
            raise _EvidenceBudgetExceeded(reason)
        if run.usage.evidence_count + len(new_refs) > run.budget.max_evidence:
            raise _EvidenceBudgetExceeded("evidence budget exhausted")
        allowed, reason = self._guard.can_investigation_accept_new_evidence(investigation)
        if not allowed:
            raise _EvidenceBudgetExceeded(reason)
        if (
            investigation.usage.evidence_count + len(new_refs)
            > investigation.budget.max_evidence
        ):
            raise _EvidenceBudgetExceeded("investigation evidence budget exhausted")
        run = run.model_copy(update={"evidence": run.evidence + new_refs})
        run = self._bump_usage(run, evidence_count=len(run.evidence))
        if self._events_pub is not None:
            self._events_pub.evidence_appended(run, added=len(new_refs), occurred_at=now)
        return run, len(new_refs)

    def _reload_pair(self, run: AgentRun) -> tuple[AgentRun, Investigation]:
        """Reload a run and its investigation after a status transition.

        ``update_investigation``/``update_agent_run`` are conditional on the
        model's own status, so a stale object whose status no longer matches the
        row would raise ``ConcurrentModification``.
        """
        run = self._store.get_agent_run(run.agent_run_id)
        investigation = self._store.get_investigation(run.investigation_id)
        return run, investigation

    def _bump_usage(self, run: AgentRun, **changes: int) -> AgentRun:
        usage = run.usage.model_copy(update=changes)
        return run.model_copy(update={"usage": usage})

    def _bump_investigation_usage(
        self, investigation: Investigation, **changes: int
    ) -> Investigation:
        usage = investigation.usage.model_copy(update=changes)
        return investigation.model_copy(update={"usage": usage})

    def _elapsed(self, run: AgentRun, now: datetime) -> int:
        if run.started_at is None:
            return 0
        return int((_iso_utc(now) - _iso_utc(run.started_at)).total_seconds())

    def _write_checkpoint(
        self, run: AgentRun, *, sequence: int, round_number: int, now: datetime
    ) -> Checkpoint:
        checkpoint = Checkpoint(
            checkpoint_id=f"cp-{run.agent_run_id}-{sequence}",
            agent_run_id=run.agent_run_id,
            sequence=sequence,
            status=run.status,
            round_number=round_number,
            usage=run.usage,
            created_at=now,
        )
        try:
            return self._store.append_checkpoint(checkpoint)
        except CheckpointConflict:
            # A resume may re-enter a round whose before-model-turn checkpoint
            # already exists; treat the append as idempotent.
            return checkpoint

    def _append_round_summary(
        self, run: AgentRun, round_number: int, provider_usage: Any, now: datetime
    ) -> None:
        from incidentlens_control_plane.investigation.store import AgentRound

        summary = AgentRound(
            agent_run_id=run.agent_run_id,
            round_number=round_number,
            status=run.status,
            provider_usage=provider_usage,
            usage=run.usage,
            stop_reason=None,
            created_at=now,
        )
        try:
            self._store.append_round(summary)
        except RoundConflict:
            # A resumed round may already have a summary; keep the first one.
            pass

    def _transition_run(
        self,
        run: AgentRun,
        target: AgentRunStatus,
        *,
        now: datetime,
        stop_reason: StopReason | None = None,
    ) -> AgentRun:
        previous = run.status.value
        updated = self._store.transition_agent_run_status(
            run.agent_run_id, target, now=now, stop_reason=stop_reason
        )
        if self._events_pub is not None:
            if previous != updated.status.value:
                self._events_pub.agent_run_status_changed(
                    updated, previous=previous, occurred_at=now
                )
            if target is AgentRunStatus.COMPLETED:
                self._events_pub.agent_run_completed(updated, occurred_at=now)
            elif target is AgentRunStatus.FAILED:
                self._events_pub.agent_run_failed(updated, occurred_at=now)
            elif target is AgentRunStatus.CANCELLED:
                self._events_pub.agent_run_cancelled(updated, occurred_at=now)
            if previous == AgentRunStatus.CREATED.value and target is AgentRunStatus.RUNNING:
                self._events_pub.agent_run_started(updated, occurred_at=now)
        return updated

    def _transition_investigation(
        self,
        investigation: Investigation,
        target: InvestigationStatus,
        *,
        now: datetime,
        stop_reason: StopReason | None = None,
    ) -> Investigation:
        previous = investigation.status.value
        updated = self._store.transition_investigation_status(
            investigation.investigation_id, target, now=now, stop_reason=stop_reason
        )
        if self._events_pub is not None and previous != updated.status.value:
            self._events_pub.investigation_status_changed(
                updated, previous=previous, occurred_at=now
            )
            if target is InvestigationStatus.COMPLETED:
                self._events_pub.investigation_completed(updated, occurred_at=now)
            elif target is InvestigationStatus.CANCELLED:
                self._events_pub.investigation_cancelled(updated, occurred_at=now)
            elif target is InvestigationStatus.FAILED:
                self._events_pub.investigation_failed(updated, occurred_at=now)
        return updated


__all__ = [
    "AgentOrchestrator",
    "RunNotFound",
]
