"""Projection from raw checkpoint dictionaries to InvestigationState."""

from __future__ import annotations

from typing import Any, Mapping

from incidentlens_contracts.models import Evidence, Hypothesis, InvestigationStatus

from incidentlens_control_plane.agent.state import InvestigationState


def project_investigation_state(raw: Mapping[str, Any]) -> InvestigationState:
    """Convert a raw checkpoint dictionary into a validated InvestigationState.

    Each ``Hypothesis`` and ``Evidence`` entry is validated individually
    so that schema errors surface early with clear messages rather than
    failing deep inside ``InvestigationState.model_validate``.

    This function does **not** fill absent required fields from a second
    database -- it expects the raw mapping to carry everything needed.
    """
    hypotheses = [Hypothesis(**h) for h in raw.get("hypotheses", [])]
    evidence = [Evidence(**e) for e in raw.get("evidence", [])]

    status_raw = raw.get("status", InvestigationStatus.SCOPING)
    if isinstance(status_raw, str):
        status = InvestigationStatus(status_raw)
    else:
        status = status_raw

    return InvestigationState(
        incident_id=raw["incident_id"],
        status=status,
        current_round=raw.get("current_round", 0),
        max_rounds=raw.get("max_rounds", 8),
        alert=raw.get("alert", {}),
        hypotheses=hypotheses,
        evidence=evidence,
        report=raw.get("report"),
        phase=raw.get("phase", "parse_alert"),
        retrieved_cases=raw.get("retrieved_cases", []),
        loaded_skill_names=raw.get("loaded_skill_names", []),
        model_profile=raw.get("model_profile", ""),
        model_call_count=raw.get("model_call_count", 0),
        tool_call_count=raw.get("tool_call_count", 0),
        fallback_used=raw.get("fallback_used", False),
        last_error_code=raw.get("last_error_code"),
        last_checkpoint_id=raw.get("last_checkpoint_id"),
        conclusion_phase=raw.get("conclusion_phase", False),
        eligible_cause_codes=raw.get("eligible_cause_codes", []),
        eligible_evidence_ids=raw.get("eligible_evidence_ids", []),
        conclusion_status=raw.get("conclusion_status", "not_ready"),
        conclusion_attempt_count=raw.get("conclusion_attempt_count", 0),
        last_report_rejection_reason=raw.get("last_report_rejection_reason"),
    )
