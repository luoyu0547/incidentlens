"""Investigation lifecycle service over the bounded agent orchestrator.

``InvestigationService`` is the thin API surface for starting, cancelling and
resuming investigations.  Cancellation is idempotent and status-based: it parks
the investigation and every non-terminal run in ``cancel_requested`` (a CREATED
run is cancelled outright), and the orchestrator loop finalises the terminal
state at its next round boundary.  Resume runs an agent run from its latest
checkpoint, re-evaluating budget/pause conditions under current state.  All
heavy lifting (rounds, checkpoints, child delegation, evidence) stays in
``AgentOrchestrator``; the service only creates investigations, delegates to the
orchestrator, and exposes read queries for the API layer (Task 8).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.approvals.types import ApprovalRecord, ApprovalStatus
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.investigation.events import InvestigationEventPublisher
from incidentlens_control_plane.investigation.orchestrator import AgentOrchestrator
from incidentlens_control_plane.investigation.provider import ToolRequest
from incidentlens_control_plane.investigation.registry_proposals import (
    RegistryProposalService,
)
from incidentlens_control_plane.investigation.state_machine import (
    AGENT_RUN_STATE_MACHINE,
    INVESTIGATION_STATE_MACHINE,
    AgentRunStatus,
    IllegalTransition,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.store import (
    AgentRound,
    Checkpoint,
    InvestigationNotFound,
    InvestigationStore,
)
from incidentlens_control_plane.investigation.tool_executor import ToolExecutor
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    Conclusion,
    Hypothesis,
    Investigation,
    InvestigationBudget,
    RegistryProposalStatus,
    RegistryUpdateProposal,
    StopReason,
    ToolCall,
)

# States from which an approval decision may resume an agent run.  A CREATED or
# RUNNING run is left alone: CREATED has never been started and RUNNING already
# has a live loop, so neither is an approval-decision resume target.
_RESUMABLE_RUN_STATUSES: frozenset[AgentRunStatus] = frozenset(
    {
        AgentRunStatus.WAITING_APPROVAL,
        AgentRunStatus.WAITING_CHILDREN,
        AgentRunStatus.PAUSED_BUDGET,
        AgentRunStatus.PAUSED_MISSING_EVIDENCE,
        AgentRunStatus.PAUSED_UNCERTAIN_STATE,
    }
)


class InvestigationService:
    """High-level lifecycle API over the bounded orchestrator."""

    def __init__(
        self,
        *,
        store: InvestigationStore,
        orchestrator: AgentOrchestrator,
        now: Callable[[], datetime] | None = None,
        approvals: ApprovalService | None = None,
        executor: ToolExecutor | None = None,
        registry_proposals: RegistryProposalService | None = None,
        events: RuntimeEventStore | None = None,
        broker: RuntimeEventBroker | None = None,
        default_investigation_budget: InvestigationBudget | None = None,
        max_active_investigations: int = 4,
    ) -> None:
        if max_active_investigations < 1:
            raise ValueError("max_active_investigations must be >= 1")
        self._store = store
        self._orchestrator = orchestrator
        self._now = now or (lambda: datetime.now(UTC))
        self._approvals = approvals
        self._executor = executor
        self._registry_proposals = registry_proposals
        self._default_investigation_budget = (
            default_investigation_budget or InvestigationBudget()
        )
        self._max_active_investigations = max_active_investigations
        # The recovery service flips this off during an orderly shutdown so no
        # new investigation can be created while active loops are draining.
        self.accepting: bool = True
        self._events_pub = (
            InvestigationEventPublisher(events, broker)
            if events is not None and broker is not None
            else None
        )

    # -- lifecycle ------------------------------------------------------------

    def create_investigation(
        self,
        *,
        project_id: str,
        target_id: str,
        service: str,
        symptom: str,
        incident_id: str | None = None,
        budget: InvestigationBudget | None = None,
    ) -> Investigation:
        """Persist a new investigation in the CREATED state.

        Refuses to create while the runtime is shutting down and enforces the
        bounded ``max_active_investigations`` cap over non-terminal
        investigations, so an unbounded fleet can never be launched.
        """
        if not self.accepting:
            raise NotAcceptingInvestigations(
                "runtime is shutting down; no new investigations are accepted"
            )
        if len(self._store.list_non_terminal_investigations()) >= (
            self._max_active_investigations
        ):
            raise TooManyActiveInvestigations(
                f"active investigations exceed max_active={self._max_active_investigations}"
            )
        now = self._now()
        investigation = Investigation(
            investigation_id=f"inv-{uuid.uuid4().hex[:16]}",
            incident_id=incident_id or f"inc-{uuid.uuid4().hex[:16]}",
            project_id=project_id,
            target_id=target_id,
            service=service,
            symptom=symptom,
            status=InvestigationStatus.CREATED,
            budget=budget or self._default_investigation_budget,
            usage=_empty_usage(),
            created_at=now,
            updated_at=now,
        )
        stored = self._store.create_investigation(investigation)
        if self._events_pub is not None:
            self._events_pub.investigation_created(stored, occurred_at=now)
        return stored

    async def start(
        self,
        investigation_id: str,
        parent_scope: AgentScope,
        *,
        parent_budget: AgentBudget | None = None,
    ) -> AgentRun:
        """Transition the investigation to RUNNING and run its parent loop.

        If a non-terminal parent run already exists, it is resumed instead of a
        second parent being created.
        """
        now = self._now()
        investigation = self._store.get_investigation(investigation_id)
        if INVESTIGATION_STATE_MACHINE.is_terminal(investigation.status):
            raise InvestigationAlreadyTerminal(
                f"investigation {investigation_id} is already terminal"
            )
        if investigation.status is not InvestigationStatus.RUNNING:
            investigation = self._store.transition_investigation_status(
                investigation_id, InvestigationStatus.RUNNING, now=now
            )
        existing = [
            run
            for run in self._store.list_agent_runs(investigation_id=investigation_id)
            if run.parent_run_id is None and not AGENT_RUN_STATE_MACHINE.is_terminal(run.status)
        ]
        if existing:
            run = await self._orchestrator.run(existing[0].agent_run_id)
        else:
            run = await self._orchestrator.run_investigation(
                investigation, parent_scope, parent_budget=parent_budget
            )
        if self._events_pub is not None:
            self._events_pub.investigation_started(investigation, run, occurred_at=now)
        return run

    async def resume_run(self, agent_run_id: str) -> AgentRun:
        """Run/resume an agent run from its latest checkpoint."""
        return await self._orchestrator.run(agent_run_id)

    async def cancel(self, investigation_id: str) -> Investigation:
        """Request cancellation of an investigation (idempotent).

        A CREATED investigation (never started) is cancelled outright; any
        other non-terminal investigation is parked in ``cancel_requested`` and
        every non-terminal run is parked too (a CREATED run is cancelled
        outright).  Runs with a live loop finalise to ``cancelled`` at their
        next round boundary.
        """
        now = self._now()
        investigation = self._store.get_investigation(investigation_id)
        if INVESTIGATION_STATE_MACHINE.is_terminal(investigation.status):
            return investigation
        previous = investigation.status.value
        if investigation.status is InvestigationStatus.CREATED:
            investigation = self._store.transition_investigation_status(
                investigation_id, InvestigationStatus.CANCELLED, now=now,
                stop_reason=StopReason.CANCELLED,
            )
            if self._events_pub is not None:
                self._events_pub.investigation_status_changed(
                    investigation, previous=previous, occurred_at=now
                )
                self._events_pub.investigation_cancelled(
                    investigation, occurred_at=now
                )
        elif investigation.status is not InvestigationStatus.CANCEL_REQUESTED:
            investigation = self._store.transition_investigation_status(
                investigation_id, InvestigationStatus.CANCEL_REQUESTED, now=now
            )
            if self._events_pub is not None:
                self._events_pub.investigation_status_changed(
                    investigation, previous=previous, occurred_at=now
                )
        for run in self._store.list_agent_runs(investigation_id=investigation_id):
            self._park_run_for_cancel(run, now=now)
        return self._store.get_investigation(investigation_id)

    async def cancel_run(self, agent_run_id: str) -> AgentRun:
        """Request cancellation of a single run (idempotent)."""
        run = self._store.get_agent_run(agent_run_id)
        self._park_run_for_cancel(run, now=self._now())
        return self._store.get_agent_run(agent_run_id)

    def _park_run_for_cancel(self, run: AgentRun, *, now: datetime) -> None:
        if AGENT_RUN_STATE_MACHINE.is_terminal(run.status):
            return
        if run.status is AgentRunStatus.CANCEL_REQUESTED:
            return
        if run.status is AgentRunStatus.CREATED:
            self._store.transition_agent_run_status(
                run.agent_run_id, AgentRunStatus.CANCELLED, now=now,
                stop_reason=StopReason.CANCELLED,
            )
            return
        self._store.transition_agent_run_status(
            run.agent_run_id, AgentRunStatus.CANCEL_REQUESTED, now=now,
            stop_reason=StopReason.CANCELLED,
        )

    async def cancel_all_active(self, *, now: datetime | None = None) -> int:
        """Park every non-terminal investigation (and its runs) for cancellation.

        Used by the recovery service on shutdown.  Returns how many
        investigations were parked.  Idempotent: terminal investigations and
        already-parked runs are left untouched.
        """
        now = now or self._now()
        count = 0
        for investigation in self._store.list_non_terminal_investigations():
            await self.cancel(investigation.investigation_id)
            count += 1
        return count

    # -- read queries ---------------------------------------------------------

    def get_investigation(self, investigation_id: str) -> Investigation:
        return self._store.get_investigation(investigation_id)

    def list_investigations(
        self,
        *,
        project_id: str | None = None,
        status: InvestigationStatus | None = None,
        incident_id: str | None = None,
    ) -> tuple[Investigation, ...]:
        return self._store.list_investigations(
            project_id=project_id, status=status, incident_id=incident_id
        )

    def get_run(self, agent_run_id: str) -> AgentRun:
        return self._store.get_agent_run(agent_run_id)

    def list_runs(
        self,
        *,
        investigation_id: str | None = None,
        parent_run_id: str | None = None,
        status: AgentRunStatus | None = None,
    ) -> tuple[AgentRun, ...]:
        return self._store.list_agent_runs(
            investigation_id=investigation_id,
            parent_run_id=parent_run_id,
            status=status,
        )

    def list_rounds(self, agent_run_id: str) -> tuple[AgentRound, ...]:
        return self._store.list_rounds(agent_run_id)

    def list_checkpoints(self, agent_run_id: str) -> tuple[Checkpoint, ...]:
        return self._store.list_checkpoints(agent_run_id)

    def list_waiting_approval_runs(self) -> tuple[AgentRun, ...]:
        return self._store.list_waiting_approval_runs()

    def list_waiting_approval_tool_calls(self) -> tuple[ToolCall, ...]:
        return self._store.list_waiting_approval_tool_calls()

    def list_children(
        self, *, parent_run_id: str, investigation_id: str | None = None
    ) -> tuple[AgentRun, ...]:
        return self._store.list_agent_runs(
            parent_run_id=parent_run_id, investigation_id=investigation_id
        )

    def get_tool_call(self, tool_call_id: str) -> ToolCall:
        return self._store.get_tool_call(tool_call_id)

    def list_hypotheses(
        self,
        *,
        investigation_id: str | None = None,
        agent_run_id: str | None = None,
    ) -> tuple[Hypothesis, ...]:
        return self._store.list_hypotheses(
            investigation_id=investigation_id, agent_run_id=agent_run_id
        )

    def list_conclusions(
        self,
        *,
        investigation_id: str | None = None,
        agent_run_id: str | None = None,
    ) -> tuple[Conclusion, ...]:
        return self._store.list_conclusions(
            investigation_id=investigation_id, agent_run_id=agent_run_id
        )

    def list_proposals(
        self,
        *,
        investigation_id: str | None = None,
        status: RegistryProposalStatus | None = None,
    ) -> tuple[RegistryUpdateProposal, ...]:
        return self._store.list_proposals(
            investigation_id=investigation_id,
            status=status,
        )

    def list_delegated_tasks(
        self,
        *,
        investigation_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> tuple[object, ...]:
        return self._store.list_delegated_tasks(
            investigation_id=investigation_id, parent_run_id=parent_run_id
        )

    # -- approval decisions ---------------------------------------------------

    async def handle_approval_decision(
        self, approval_id: str, *, now: datetime | None = None
    ) -> ApprovalDecisionOutcome:
        """Resolve an approval decision against a matching tool call or proposal.

        An approval granted to a WAITING_APPROVAL tool call re-executes the tool
        with the exact, single-use approval (consumed by the underlying gateway)
        and resumes the run; a rejected one parks the tool call as cancelled and
        resumes the run so the agent can react.  A pending registry proposal
        whose canonical intent hash matches the approval is delegated to
        ``RegistryProposalService.handle_approval_decision`` and its run resumed
        when parked.  Approvals that match nothing are a no-op: the record is
        already decided and the linkage is best-effort.
        """
        now = now or self._now()
        if self._approvals is None:
            return ApprovalDecisionOutcome(approval_id=approval_id, matched="none")
        approval = self._approvals.get(approval_id)
        if approval is None:
            return ApprovalDecisionOutcome(approval_id=approval_id, matched="none")

        for tool_call in self._store.list_waiting_approval_tool_calls():
            if tool_call.approval_id == approval_id:
                return await self._handle_tool_approval(tool_call, approval, now)

        if self._registry_proposals is not None:
            for proposal in self._store.list_pending_proposals():
                if proposal.approval_intent_sha256 == approval.intent_sha256:
                    return await self._handle_proposal_approval(proposal, approval, now)

        return ApprovalDecisionOutcome(approval_id=approval_id, matched="none")

    async def _handle_tool_approval(
        self,
        tool_call: ToolCall,
        approval: ApprovalRecord,
        now: datetime,
    ) -> ApprovalDecisionOutcome:
        if approval.status is ApprovalStatus.REJECTED:
            self._store.transition_tool_call_status(
                tool_call.tool_call_id, ToolCallStatus.CANCELLED, now=now
            )
            await self._resume_after_decision(tool_call.agent_run_id, now)
            return ApprovalDecisionOutcome(
                approval_id=approval.approval_id,
                matched="tool_call",
                tool_call_id=tool_call.tool_call_id,
                run_id=tool_call.agent_run_id,
                action="cancelled",
            )
        if approval.status is not ApprovalStatus.APPROVED or self._executor is None:
            return ApprovalDecisionOutcome(
                approval_id=approval.approval_id,
                matched="tool_call",
                tool_call_id=tool_call.tool_call_id,
                run_id=tool_call.agent_run_id,
                action="noop",
            )
        run = self._store.get_agent_run(tool_call.agent_run_id)
        if self._run_cancel_pending(run):
            # C2: an approval landing after the run was cancelled must never
            # re-execute the tool; park the call and let the cancel finalise.
            self._store.transition_tool_call_status(
                tool_call.tool_call_id,
                ToolCallStatus.CANCELLED,
                now=now,
                error_redacted="run cancelled before the approval could execute",
            )
            return ApprovalDecisionOutcome(
                approval_id=approval.approval_id,
                matched="tool_call",
                tool_call_id=tool_call.tool_call_id,
                run_id=tool_call.agent_run_id,
                action="cancelled",
            )
        request = ToolRequest(
            tool_call_id=tool_call.tool_call_id,
            tool_name=tool_call.tool_name,
            arguments=tool_call.arguments,
        )
        # C1: persist the re-execution before the executor runs so a crash
        # mid-execution leaves a RUNNING call for startup recovery to classify
        # (a dangerous call is parked UNCERTAIN, never replayed).
        self._store.transition_tool_call_status(
            tool_call.tool_call_id,
            ToolCallStatus.RUNNING,
            now=now,
        )
        outcome = await self._executor.execute(
            request, run, approval_id=tool_call.approval_id, now=now
        )
        if outcome.status is ToolCallStatus.WAITING_APPROVAL:
            # The exact approval could not be consumed (e.g. already spent):
            # surface the call as failed rather than looping on approval.
            self._store.transition_tool_call_status(
                tool_call.tool_call_id,
                ToolCallStatus.FAILED,
                now=now,
                error_redacted=outcome.error_redacted or "approval could not be consumed",
            )
        else:
            # The call was stamped RUNNING before execution; land it on the
            # executed outcome.
            self._store.transition_tool_call_status(
                tool_call.tool_call_id,
                outcome.status,
                now=now,
                output_bytes=outcome.output_bytes,
                evidence_ids=tuple(ref.evidence_id for ref in outcome.evidence),
                error_redacted=outcome.error_redacted,
            )
            self._append_tool_outcome_evidence(
                self._store.get_agent_run(tool_call.agent_run_id), outcome, now
            )
        if outcome.status is ToolCallStatus.UNCERTAIN:
            # Mirror the main loop: an approved tool whose result could not be
            # confirmed parks the run (and its parent investigation)
            # PAUSED_UNCERTAIN_STATE instead of resuming it.
            self._park_uncertain_after_tool(
                self._store.get_agent_run(tool_call.agent_run_id), now
            )
            return ApprovalDecisionOutcome(
                approval_id=approval.approval_id,
                matched="tool_call",
                tool_call_id=tool_call.tool_call_id,
                run_id=tool_call.agent_run_id,
                action="uncertain",
                applied=False,
                consumed=True,
            )
        await self._resume_after_decision(tool_call.agent_run_id, now)
        return ApprovalDecisionOutcome(
            approval_id=approval.approval_id,
            matched="tool_call",
            tool_call_id=tool_call.tool_call_id,
            run_id=tool_call.agent_run_id,
            action="re-executed",
            applied=outcome.status is ToolCallStatus.SUCCEEDED,
            consumed=True,
        )

    async def _handle_proposal_approval(
        self,
        proposal: RegistryUpdateProposal,
        approval: ApprovalRecord,
        now: datetime,
    ) -> ApprovalDecisionOutcome:
        assert self._registry_proposals is not None
        run = self._store.get_agent_run(proposal.agent_run_id)
        if self._run_cancel_pending(run):
            # C2: never apply a registry write for a run the operator cancelled.
            self._store.transition_proposal_status(
                proposal.proposal_id, RegistryProposalStatus.STALE, now=now
            )
            return ApprovalDecisionOutcome(
                approval_id=approval.approval_id,
                matched="registry_proposal",
                proposal_id=proposal.proposal_id,
                run_id=proposal.agent_run_id,
                action="cancelled",
            )
        outcome = await self._registry_proposals.handle_approval_decision(
            proposal, approval, now=now
        )
        await self._resume_after_decision(proposal.agent_run_id, now)
        return ApprovalDecisionOutcome(
            approval_id=approval.approval_id,
            matched="registry_proposal",
            proposal_id=proposal.proposal_id,
            run_id=proposal.agent_run_id,
            action=outcome.decision or "noop",
            applied=outcome.applied,
        )

    def _run_cancel_pending(self, run: AgentRun) -> bool:
        """Return True when an approval decision must not execute for *run*.

        A run parked for cancellation, a run whose owning investigation is
        parked for cancellation or already terminal, and any terminal run must
        never have an approval re-execute a dangerous tool or apply a registry
        write: the operator's cancel wins over the approval decision.  A crash
        mid-cancel can leave a WAITING_APPROVAL run underneath a CANCELLED or
        CANCEL_REQUESTED investigation, so the investigation is checked too.
        """
        if run.status is AgentRunStatus.CANCEL_REQUESTED:
            return True
        if AGENT_RUN_STATE_MACHINE.is_terminal(run.status):
            return True
        try:
            investigation = self._store.get_investigation(run.investigation_id)
        except InvestigationNotFound:
            # A run with no owning investigation must never execute a mutation.
            return True
        return (
            investigation.status is InvestigationStatus.CANCEL_REQUESTED
            or INVESTIGATION_STATE_MACHINE.is_terminal(investigation.status)
        )

    async def _resume_after_decision(self, agent_run_id: str, now: datetime) -> AgentRun:
        """Resume a parked run after an approval decision.

        Only WAITING_APPROVAL / WAITING_CHILDREN / PAUSED_* runs are resumed; a
        CREATED run has never been started and a RUNNING run already has a live
        loop, so neither is a resume target.
        """
        run = self._store.get_agent_run(agent_run_id)
        if run.status not in _RESUMABLE_RUN_STATUSES:
            return run
        if run.status is AgentRunStatus.WAITING_APPROVAL:
            # The orchestrator parks WAITING_APPROVAL loops permanently; move the
            # run back to RUNNING (and its investigation) before re-entering.
            previous = run.status.value
            run = self._store.transition_agent_run_status(
                run.agent_run_id, AgentRunStatus.RUNNING, now=now
            )
            if self._events_pub is not None:
                self._events_pub.agent_run_status_changed(
                    run, previous=previous, occurred_at=now
                )
                self._events_pub.agent_run_started(run, occurred_at=now)
            self._restore_investigation_running(run.investigation_id, now)
        return await self._orchestrator.run(agent_run_id)

    def _restore_investigation_running(self, investigation_id: str, now: datetime) -> None:
        investigation = self._store.get_investigation(investigation_id)
        if investigation.status is InvestigationStatus.WAITING_APPROVAL:
            previous = investigation.status.value
            updated = self._store.transition_investigation_status(
                investigation_id, InvestigationStatus.RUNNING, now=now
            )
            if self._events_pub is not None:
                self._events_pub.investigation_status_changed(
                    updated, previous=previous, occurred_at=now
                )

    def _park_uncertain_after_tool(self, run: AgentRun, now: datetime) -> None:
        """Park a run and its parent investigation as PAUSED_UNCERTAIN_STATE.

        Mirrors the orchestrator's main-loop handling of an UNCERTAIN tool
        outcome so an approval re-execution whose result could not be confirmed
        parks the run instead of auto-resuming it.  The state machine forbids a
        direct WAITING_APPROVAL -> PAUSED_UNCERTAIN_STATE move, so the run is
        first moved to RUNNING exactly like ``_resume_after_decision`` does,
        then parked.
        """
        try:
            current = self._store.get_agent_run(run.agent_run_id)
            previous = current.status.value
            if current.status is AgentRunStatus.WAITING_APPROVAL:
                current = self._store.transition_agent_run_status(
                    current.agent_run_id, AgentRunStatus.RUNNING, now=now
                )
            if current.status is AgentRunStatus.RUNNING:
                updated = self._store.transition_agent_run_status(
                    current.agent_run_id,
                    AgentRunStatus.PAUSED_UNCERTAIN_STATE,
                    now=now,
                    stop_reason=StopReason.UNCERTAIN_STATE,
                )
                if self._events_pub is not None:
                    self._events_pub.agent_run_status_changed(
                        updated, previous=previous, occurred_at=now
                    )
        except IllegalTransition:
            pass
        if run.kind is not AgentRunKind.PARENT:
            return
        try:
            investigation = self._store.get_investigation(run.investigation_id)
            previous = investigation.status.value
            current = investigation
            if current.status is InvestigationStatus.WAITING_APPROVAL:
                current = self._store.transition_investigation_status(
                    current.investigation_id, InvestigationStatus.RUNNING, now=now
                )
            if current.status is InvestigationStatus.RUNNING:
                updated = self._store.transition_investigation_status(
                    current.investigation_id,
                    InvestigationStatus.PAUSED_UNCERTAIN_STATE,
                    now=now,
                    stop_reason=StopReason.UNCERTAIN_STATE,
                )
                if self._events_pub is not None:
                    self._events_pub.investigation_status_changed(
                        updated, previous=previous, occurred_at=now
                    )
        except (InvestigationNotFound, IllegalTransition):
            pass

    def _append_tool_outcome_evidence(
        self, run: AgentRun, outcome: object, now: datetime
    ) -> None:
        """Fold a re-executed tool's evidence refs into the run, bounded."""
        new_refs = tuple(
            ref
            for ref in outcome.evidence
            if ref.evidence_id not in {known.evidence_id for known in run.evidence}
        )
        if not new_refs:
            return
        if run.usage.evidence_count + len(new_refs) > run.budget.max_evidence:
            return
        run = run.model_copy(update={"evidence": run.evidence + new_refs})
        usage = run.usage.model_copy(
            update={
                "evidence_count": run.usage.evidence_count + len(new_refs),
                "tool_calls": run.usage.tool_calls + 1,
                "total_output_bytes": run.usage.total_output_bytes + outcome.output_bytes,
            }
        )
        run = run.model_copy(update={"usage": usage})
        self._store.update_agent_run(run)
        if self._events_pub is not None:
            self._events_pub.evidence_appended(
                run, added=len(new_refs), occurred_at=now
            )


@dataclass(frozen=True)
class ApprovalDecisionOutcome:
    """The result of resolving one approval decision against the investigation."""

    approval_id: str
    matched: str
    tool_call_id: str | None = None
    proposal_id: str | None = None
    run_id: str | None = None
    action: str | None = None
    applied: bool = False
    consumed: bool = False


class InvestigationAlreadyTerminal(Exception):
    """Raised when ``start`` targets an investigation in a terminal state."""


class TooManyActiveInvestigations(Exception):
    """Raised when creating an investigation would exceed the active cap."""


class NotAcceptingInvestigations(Exception):
    """Raised when the runtime is shutting down and refuses new investigations."""


def _empty_usage():
    from incidentlens_control_plane.investigation.types import UsageCounters

    return UsageCounters()


__all__ = [
    "ApprovalDecisionOutcome",
    "InvestigationAlreadyTerminal",
    "InvestigationService",
    "NotAcceptingInvestigations",
    "TooManyActiveInvestigations",
]
