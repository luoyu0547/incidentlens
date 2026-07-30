"""Pure-function conclusion readiness evaluation.

Provides provider-agnostic evidence-driven readiness checks that determine
whether the agent has collected sufficient material to propose a root cause.
The module intentionally does NOT select a final root cause -- it only
returns candidate cause codes and eligible evidence IDs.

Public API:
  - evaluate_conclusion_readiness(incident_id, loaded_skill_names, evidence, policies)
      -> ConclusionReadiness
  - parse_proposal(tool_calls) -> RootCauseProposal | None
"""

from __future__ import annotations

from typing import Any

from incidentlens_contracts.models import Evidence

from incidentlens_control_plane.agent.types import ConclusionReadiness, RootCauseProposal


def evaluate_conclusion_readiness(
    *,
    incident_id: str,
    loaded_skill_names: list[str],
    evidence: list[Evidence],
    policies: dict[str, Any],
) -> ConclusionReadiness:
    """Determine whether the agent has collected enough evidence to propose a root cause.

    Parameters
    ----------
    incident_id:
        The current incident ID.  Evidence from other incidents is ignored.
    loaded_skill_names:
        Names of skills the agent has already read via ``read_file``.
    evidence:
        All evidence collected during the current investigation.
    policies:
        Mapping of ``cause_code -> EvidencePolicy`` for loaded skills.

    Returns
    -------
    ConclusionReadiness with:
      - ``ready``: True when at least one cause code has sufficient
        independent material evidence from the current incident.
      - ``eligible_cause_codes``: cause codes that meet their evidence policy.
      - ``eligible_evidence_ids``: evidence IDs that contribute to any
        eligible cause code.
    """
    if not evidence or not policies:
        return ConclusionReadiness(
            ready=False,
            eligible_cause_codes=[],
            eligible_evidence_ids=[],
        )

    # Build owned evidence lookup (current incident only)
    owned_by_incident: dict[str, Evidence] = {}
    for ev in evidence:
        ev_id = ev.id
        # Check if this evidence belongs to the current incident
        ev_content = ev.content
        if isinstance(ev_content, dict) and ev_content.get("incident_id") == incident_id:
            owned_by_incident[ev_id] = ev
        # Also include evidence without incident_id in content (backward compat)
        elif isinstance(ev_content, dict) and "incident_id" not in ev_content:
            owned_by_incident[ev_id] = ev

    if not owned_by_incident:
        return ConclusionReadiness(
            ready=False,
            eligible_cause_codes=[],
            eligible_evidence_ids=[],
        )

    # Map source_tool -> list of evidence IDs
    evidence_by_source: dict[str, list[str]] = {}
    for ev_id, ev in owned_by_incident.items():
        source = ev.source_tool
        evidence_by_source.setdefault(source, []).append(ev_id)

    loaded_skill_set = set(loaded_skill_names)
    eligible_cause_codes: list[str] = []
    eligible_evidence_ids: set[str] = set()

    for cause_code, policy in policies.items():
        # Must have loaded the owning skill
        skill_name = getattr(policy, "skill_name", "")
        if skill_name not in loaded_skill_set:
            continue

        # Must not have skill load failure
        # (checked at caller level, but guard here too)

        required_types = set(
            getattr(policy, "required_evidence_types", [])
            or []
        )
        min_independent = getattr(policy, "minimum_independent_evidence", 0)

        # Check which required evidence types are satisfied
        satisfied_types: set[str] = set()
        contributing_ids: set[str] = set()
        for source_tool, ev_ids in evidence_by_source.items():
            if source_tool in required_types:
                satisfied_types.add(source_tool)
                contributing_ids.update(ev_ids)

        # Must have minimum independent evidence types
        if len(satisfied_types) < min_independent:
            continue

        # Check for direct contradictions across ALL owned evidence
        contradictions = set(
            getattr(policy, "direct_contradictions", [])
            or []
        )
        has_contradiction = False
        for ev_id, ev in owned_by_incident.items():
            if ev.source_tool in contradictions:
                has_contradiction = True
                break

        if has_contradiction:
            continue

        eligible_cause_codes.append(cause_code)
        eligible_evidence_ids.update(contributing_ids)

    return ConclusionReadiness(
        ready=len(eligible_cause_codes) > 0,
        eligible_cause_codes=eligible_cause_codes,
        eligible_evidence_ids=sorted(eligible_evidence_ids),
    )


def parse_proposal(tool_calls: list[dict[str, Any]]) -> RootCauseProposal | None:
    """Parse a RootCauseProposal from model tool calls.

    Returns the first valid proposal found, or None if no valid proposal exists.
    Only the first RootCauseProposal tool call is considered; additional ones
    are ignored.
    """
    for tc in tool_calls:
        if tc.get("name") != "RootCauseProposal":
            continue
        args = tc.get("args", {})
        try:
            proposal = RootCauseProposal.model_validate(args)
            return proposal
        except Exception:
            continue
    return None
