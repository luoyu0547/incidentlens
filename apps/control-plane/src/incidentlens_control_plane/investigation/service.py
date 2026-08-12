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
from datetime import UTC, datetime

from incidentlens_control_plane.investigation.orchestrator import AgentOrchestrator
from incidentlens_control_plane.investigation.state_machine import (
    AGENT_RUN_STATE_MACHINE,
    INVESTIGATION_STATE_MACHINE,
    AgentRunStatus,
    InvestigationStatus,
)
from incidentlens_control_plane.investigation.store import (
    AgentRound,
    Checkpoint,
    InvestigationStore,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentScope,
    Investigation,
    InvestigationBudget,
    StopReason,
    ToolCall,
)


class InvestigationService:
    """High-level lifecycle API over the bounded orchestrator."""

    def __init__(
        self,
        *,
        store: InvestigationStore,
        orchestrator: AgentOrchestrator,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._orchestrator = orchestrator
        self._now = now or (lambda: datetime.now(UTC))

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
        """Persist a new investigation in the CREATED state."""
        now = self._now()
        investigation = Investigation(
            investigation_id=f"inv-{uuid.uuid4().hex[:16]}",
            incident_id=incident_id or f"inc-{uuid.uuid4().hex[:16]}",
            project_id=project_id,
            target_id=target_id,
            service=service,
            symptom=symptom,
            status=InvestigationStatus.CREATED,
            budget=budget or InvestigationBudget(),
            usage=_empty_usage(),
            created_at=now,
            updated_at=now,
        )
        return self._store.create_investigation(investigation)

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
            return await self._orchestrator.run(existing[0].agent_run_id)
        return await self._orchestrator.run_investigation(
            investigation, parent_scope, parent_budget=parent_budget
        )

    async def resume_run(self, agent_run_id: str) -> AgentRun:
        """Run/resume an agent run from its latest checkpoint."""
        return await self._orchestrator.run(agent_run_id)

    async def cancel(self, investigation_id: str) -> Investigation:
        """Request cancellation of an investigation (idempotent).

        Parks the investigation and every non-terminal run in
        ``cancel_requested``; a CREATED run is cancelled outright.  Runs with a
        live loop finalise to ``cancelled`` at their next round boundary.
        """
        now = self._now()
        investigation = self._store.get_investigation(investigation_id)
        if INVESTIGATION_STATE_MACHINE.is_terminal(investigation.status):
            return investigation
        if investigation.status is not InvestigationStatus.CANCEL_REQUESTED:
            investigation = self._store.transition_investigation_status(
                investigation_id, InvestigationStatus.CANCEL_REQUESTED, now=now
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


class InvestigationAlreadyTerminal(Exception):
    """Raised when ``start`` targets an investigation in a terminal state."""


def _empty_usage():
    from incidentlens_control_plane.investigation.types import UsageCounters

    return UsageCounters()


__all__ = [
    "InvestigationAlreadyTerminal",
    "InvestigationService",
]
