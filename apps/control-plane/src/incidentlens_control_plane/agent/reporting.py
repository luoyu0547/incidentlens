"""Report generation for completed investigations.

Guards:
  - Report is only generated when a root cause is verified
    (at least one CONFIRMED hypothesis exists).
  - Report contains structured findings with root_cause and evidence summary.
"""

from __future__ import annotations

from typing import Any

from incidentlens_contracts.models import (
    Evidence,
    HypothesisStatus,
)

from incidentlens_control_plane.agent.state import InvestigationState


def can_generate_report(state: InvestigationState) -> bool:
    """Check whether a report can be generated.

    A report can only be generated when at least one hypothesis
    is CONFIRMED (root cause verified).
    """
    return any(
        h.status == HypothesisStatus.CONFIRMED for h in state.hypotheses
    )


def generate_report(state: InvestigationState) -> dict[str, Any]:
    """Generate a structured investigation report.

    Returns a dict with:
      - root_cause: description of the confirmed hypothesis
      - findings: list of evidence summaries
      - hypotheses: all hypotheses with their final status
      - rounds_completed: number of rounds executed
    """
    confirmed = [
        h for h in state.hypotheses if h.status == HypothesisStatus.CONFIRMED
    ]
    ruled_out = [
        h for h in state.hypotheses if h.status == HypothesisStatus.RULED_OUT
    ]
    active = [
        h for h in state.hypotheses if h.status == HypothesisStatus.ACTIVE
    ]

    # Build evidence index for lookups
    evidence_by_id: dict[str, Evidence] = {
        e.id: e for e in state.evidence
    }

    # Build findings from confirmed hypothesis evidence
    findings: list[dict[str, Any]] = []
    for hyp in confirmed:
        for ev_id in hyp.supporting_evidence_ids:
            ev = evidence_by_id.get(ev_id)
            if ev:
                findings.append({
                    "hypothesis_id": hyp.id,
                    "evidence_id": ev.id,
                    "source_tool": ev.source_tool,
                    "content": ev.content,
                })

    root_cause = confirmed[0].description if confirmed else "No confirmed root cause"

    return {
        "root_cause": root_cause,
        "findings": findings,
        "hypotheses": {
            "confirmed": [h.model_dump() for h in confirmed],
            "ruled_out": [h.model_dump() for h in ruled_out],
            "active": [h.model_dump() for h in active],
        },
        "rounds_completed": state.current_round,
        "incident_id": state.incident_id,
    }
