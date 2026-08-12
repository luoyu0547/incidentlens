"""Deterministic guardrails around a model-driven investigation loop.

The guard is pure: it never touches the provider, the tool layer, the store or
the orchestrator. It answers two questions with ``(allowed, reason)`` tuples —
``may the loop proceed?`` (pre-execution checks plus budget boundaries) and
``may the model's structured output be accepted?`` (evidence ownership). Every
reject reason is stable prose the orchestrator maps to a pause status and a
``StopReason``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from incidentlens_control_plane.investigation.state_machine import (
    AGENT_RUN_STATE_MACHINE,
    INVESTIGATION_STATE_MACHINE,
    AgentRunStatus,
    InvestigationStatus,
)
from incidentlens_control_plane.investigation.types import (
    AgentRun,
    AgentRunKind,
    ChildReport,
    Conclusion,
    Hypothesis,
    Investigation,
)


class InvestigationGuard:
    """Enforce budgets, statuses and evidence citations outside the model prompt."""

    # -- pre-execution checks -------------------------------------------------

    def check_before_model_turn(
        self,
        run: AgentRun,
        *,
        now: datetime,
    ) -> tuple[bool, str]:
        """Refuse a provider call when the run is not executable or a budget is gone."""
        status = self._check_executable(run)
        if status[0] is False:
            return status
        if run.usage.rounds >= run.budget.max_rounds:
            return False, "round budget exhausted"
        stalled = self.is_stalled_no_new_evidence(run)
        if stalled[0] is False:
            return stalled
        return self._check_wall_clock(run, now)

    def check_before_tool_execution(
        self,
        run: AgentRun,
        *,
        now: datetime,
    ) -> tuple[bool, str]:
        """Refuse a tool call when the run is not executable or a budget is gone."""
        status = self._check_executable(run)
        if status[0] is False:
            return status
        if run.usage.tool_calls >= run.budget.max_tool_calls:
            return False, "tool-call budget exhausted"
        return self._check_wall_clock(run, now)

    # -- investigation-level pre-execution checks -----------------------------

    def check_investigation_before_model_turn(
        self,
        investigation: Investigation,
        *,
        now: datetime,
    ) -> tuple[bool, str]:
        """Refuse a provider call when the investigation's global budgets are gone."""
        status = self._check_investigation_executable(investigation)
        if status[0] is False:
            return status
        if investigation.usage.rounds >= investigation.budget.max_rounds:
            return False, "investigation round budget exhausted"
        stalled = self.is_investigation_stalled_no_new_evidence(investigation)
        if stalled[0] is False:
            return stalled
        return self._check_investigation_wall_clock(investigation, now)

    def check_investigation_before_tool_execution(
        self,
        investigation: Investigation,
        *,
        now: datetime,
    ) -> tuple[bool, str]:
        """Refuse a tool call when the investigation's global budgets are gone."""
        status = self._check_investigation_executable(investigation)
        if status[0] is False:
            return status
        if investigation.usage.tool_calls >= investigation.budget.max_tool_calls:
            return False, "investigation tool-call budget exhausted"
        return self._check_investigation_wall_clock(investigation, now)

    def can_investigation_accept_output(
        self,
        investigation: Investigation,
        output_bytes: int,
    ) -> tuple[bool, str]:
        """Bound the cumulative output across all runs of the investigation."""
        if output_bytes < 0:
            return False, "output_bytes must not be negative"
        if (
            investigation.usage.total_output_bytes + output_bytes
            > investigation.budget.max_total_output_bytes
        ):
            return False, "investigation cumulative output budget exceeded"
        return True, "investigation output within budget"

    def can_investigation_accept_new_evidence(
        self,
        investigation: Investigation,
    ) -> tuple[bool, str]:
        """Refuse to grow the investigation's evidence set past its budget."""
        if investigation.usage.evidence_count >= investigation.budget.max_evidence:
            return False, "investigation evidence budget exhausted"
        return True, "investigation evidence budget available"

    def is_investigation_stalled_no_new_evidence(
        self,
        investigation: Investigation,
    ) -> tuple[bool, str]:
        """Signal the missing-evidence pause once global dry rounds pass the cap."""
        if (
            investigation.usage.consecutive_no_new_evidence_rounds
            >= investigation.budget.max_no_new_evidence_rounds
        ):
            return False, "investigation no-new-evidence budget exhausted"
        return True, "investigation no-new-evidence budget available"

    # -- budget checks --------------------------------------------------------

    def can_accept_output(
        self,
        run: AgentRun,
        output_bytes: int,
    ) -> tuple[bool, str]:
        """Bound both a single tool's output and the cumulative run output."""
        if output_bytes < 0:
            return False, "output_bytes must not be negative"
        if output_bytes > run.budget.max_output_bytes_per_tool:
            return False, "per-tool output budget exceeded"
        if run.usage.total_output_bytes + output_bytes > run.budget.max_total_output_bytes:
            return False, "cumulative output budget exceeded"
        return True, "output within budget"

    def can_spawn_child(
        self,
        run: AgentRun,
        investigation: Investigation,
    ) -> tuple[bool, str]:
        """Only a parent run may delegate, and only within the child budget."""
        if run.kind is AgentRunKind.CHILD:
            return False, "child run must not delegate grandchildren"
        if investigation.usage.children >= investigation.budget.max_children:
            return False, "child budget exhausted"
        return True, "child budget available"

    def can_accept_new_evidence(
        self,
        run: AgentRun,
    ) -> tuple[bool, str]:
        """Refuse to grow the run's evidence set past its budget."""
        if run.usage.evidence_count >= run.budget.max_evidence:
            return False, "evidence budget exhausted"
        return True, "evidence budget available"

    def is_stalled_no_new_evidence(
        self,
        run: AgentRun,
    ) -> tuple[bool, str]:
        """Signal the missing-evidence pause once consecutive dry rounds pass the cap."""
        if (
            run.usage.consecutive_no_new_evidence_rounds
            >= run.budget.max_no_new_evidence_rounds
        ):
            return False, "no-new-evidence budget exhausted"
        return True, "no-new-evidence budget available"

    # -- evidence-grounding validation ----------------------------------------

    def validate_conclusion(
        self,
        run: AgentRun,
        conclusion: Conclusion,
    ) -> tuple[bool, str]:
        """Reject conclusions that cite fabricated or out-of-run evidence."""
        return self._validate_citations(run, conclusion.evidence_ids, "conclusion")

    def validate_hypothesis(
        self,
        run: AgentRun,
        hypothesis: Hypothesis,
    ) -> tuple[bool, str]:
        """Reject hypotheses that cite fabricated or out-of-run evidence."""
        return self._validate_citations(run, hypothesis.evidence_ids, "hypothesis")

    def validate_child_report(
        self,
        run: AgentRun,
        report: ChildReport,
    ) -> tuple[bool, str]:
        """Reject child reports that cite fabricated or out-of-run evidence."""
        return self._validate_citations(run, report.evidence_ids, "child report")

    # -- helpers --------------------------------------------------------------

    def _check_executable(self, run: AgentRun) -> tuple[bool, str]:
        if AGENT_RUN_STATE_MACHINE.is_terminal(run.status):
            return False, "run is terminal"
        if run.status is not AgentRunStatus.RUNNING:
            return False, f"run is not executable in status {run.status.value!r}"
        return True, "run is executable"

    def _check_wall_clock(self, run: AgentRun, now: datetime) -> tuple[bool, str]:
        if run.started_at is not None:
            elapsed = (now.astimezone(UTC) - run.started_at.astimezone(UTC)).total_seconds()
            if elapsed >= run.budget.max_wall_clock_seconds:
                return False, "wall-clock budget exhausted"
        return True, "wall-clock budget available"

    def _check_investigation_executable(
        self, investigation: Investigation
    ) -> tuple[bool, str]:
        if INVESTIGATION_STATE_MACHINE.is_terminal(investigation.status):
            return False, "investigation is terminal"
        if investigation.status is not InvestigationStatus.RUNNING:
            return False, (
                f"investigation is not executable in status "
                f"{investigation.status.value!r}"
            )
        return True, "investigation is executable"

    def _check_investigation_wall_clock(
        self, investigation: Investigation, now: datetime
    ) -> tuple[bool, str]:
        if investigation.started_at is not None:
            elapsed = (
                now.astimezone(UTC) - investigation.started_at.astimezone(UTC)
            ).total_seconds()
            if elapsed >= investigation.budget.max_wall_clock_seconds:
                return False, "investigation wall-clock budget exhausted"
        return True, "investigation wall-clock budget available"

    def _validate_citations(
        self,
        run: AgentRun,
        evidence_ids: tuple[str, ...],
        subject: str,
    ) -> tuple[bool, str]:
        known = {evidence.evidence_id for evidence in run.evidence}
        if not evidence_ids:
            return False, f"{subject} cites no evidence"
        missing = set(evidence_ids) - known
        if missing:
            return False, (
                f"{subject} cites evidence not collected in this investigation"
            )
        return True, f"{subject} is grounded in current evidence"
