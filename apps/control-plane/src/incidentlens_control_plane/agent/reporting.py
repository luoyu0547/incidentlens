"""Report generation for completed investigations.

Guards:
  - Report is only generated when a root cause is verified
    (at least one CONFIRMED hypothesis exists).
  - The confirmed hypothesis must have a root_service.
  - All supporting evidence_ids must be owned by the current incident.
  - Report contains root_service, root_cause (cause_code), evidence_ids,
    findings, rounds_completed, and uncertainty.
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

    A report can only be generated when:
      1. At least one hypothesis is CONFIRMED.
      2. The confirmed hypothesis has a non-empty root_service.
      3. All supporting evidence_ids of the confirmed hypothesis are
         owned by the current incident (present in state.evidence).
    """
    confirmed = [
        h for h in state.hypotheses if h.status == HypothesisStatus.CONFIRMED
    ]
    if not confirmed:
        return False

    primary = confirmed[0]

    # Must have a root_service
    if not primary.root_service:
        return False

    # All supporting evidence must be owned by this incident
    owned_ids = {e.id for e in state.evidence}
    if not set(primary.supporting_evidence_ids) <= owned_ids:
        return False

    return True


def generate_report(state: InvestigationState) -> dict[str, Any]:
    """Generate a structured investigation report.

    Returns a dict with:
      - root_service: the confirmed root service
      - root_cause: the cause code from the confirmed hypothesis
      - evidence_ids: list of evidence IDs supporting the confirmed hypothesis
      - findings: list of evidence summaries
      - hypotheses: all hypotheses with their final status
      - rounds_completed: number of rounds executed
      - incident_id: the incident identifier
      - uncertainty: confidence gap or uncertainty measure
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

    primary = confirmed[0] if confirmed else None
    root_cause = primary.cause_code if primary and primary.cause_code else (
        primary.description if primary else "No confirmed root cause"
    )
    root_service = primary.root_service if primary else ""
    evidence_ids = primary.supporting_evidence_ids if primary else []

    # Compute uncertainty: 1.0 - confidence of primary confirmed hypothesis
    uncertainty = 1.0 - primary.confidence if primary else 1.0

    return {
        "root_service": root_service,
        "root_cause": root_cause,
        "evidence_ids": evidence_ids,
        "findings": findings,
        "hypotheses": {
            "confirmed": [h.model_dump() for h in confirmed],
            "ruled_out": [h.model_dump() for h in ruled_out],
            "active": [h.model_dump() for h in active],
        },
        "rounds_completed": state.current_round,
        "incident_id": state.incident_id,
        "uncertainty": round(uncertainty, 3),
    }
