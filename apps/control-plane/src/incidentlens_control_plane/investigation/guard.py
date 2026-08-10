"""Deterministic guardrails around a model-driven investigation loop."""

from __future__ import annotations

from incidentlens_control_plane.investigation.types import (
    InvestigationState,
    ProposedConclusion,
)


class InvestigationGuard:
    """Enforce budgets and evidence citations outside the model prompt."""

    def can_request_another_operation(self, state: InvestigationState) -> tuple[bool, str]:
        if state.round_number >= state.max_rounds:
            return False, "investigation round budget exhausted"
        if state.tool_calls >= state.max_tool_calls:
            return False, "tool-call budget exhausted"
        return True, "operation budget available"

    def validate_conclusion(
        self,
        state: InvestigationState,
        conclusion: ProposedConclusion,
    ) -> tuple[bool, str]:
        known_evidence = {evidence.evidence_id for evidence in state.evidence}
        missing = set(conclusion.evidence_ids) - known_evidence
        if missing:
            return False, "conclusion cites evidence not collected in this investigation"
        return True, "conclusion is grounded in current evidence"
