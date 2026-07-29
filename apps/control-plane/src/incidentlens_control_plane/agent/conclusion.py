"""Deterministic conclusion readiness and proposal parsing.

This module provides provider-neutral logic for:
  - classifying material Evidence
  - evaluating Skill policy eligibility for conclusion
  - constructing bounded conclusion context for the model
  - parsing and validating RootCauseProposal output
  - classifying repair vs terminal failures

The model remains the author of root_service, cause_code, evidence_ids,
confidence, and next_action.  This module does NOT choose the final cause
or construct a proposal — it only determines readiness and validates output.
"""

from __future__ import annotations

from typing import Any

from incidentlens_contracts.models import Evidence
from pydantic import BaseModel, Field, ValidationError

from incidentlens_control_plane.agent.skills import EvidencePolicy
from incidentlens_control_plane.agent.types import RootCauseProposal

# ---------------------------------------------------------------------------
# Material evidence classification
# ---------------------------------------------------------------------------


def is_material_evidence(
    evidence: Evidence,
    incident_id: str,
) -> bool:
    """Return True if *evidence* is material for conclusion readiness.

    An evidence item is material only when:
      - its tool outcome is successful (no "error" outcome)
      - its ``data`` / ``content`` is non-empty
      - it is not synthetic invalid-argument evidence
      - it belongs to the current incident
    """
    content = evidence.content

    # Must belong to the current incident
    if content.get("incident_id") and content["incident_id"] != incident_id:
        return False

    # Reject synthetic invalid-argument evidence
    if content.get("outcome") == "invalid_arguments":
        return False

    # Reject error outcomes
    if content.get("outcome") == "error":
        return False

    # Must have non-empty data
    if not content.get("data") and not content.get("count"):
        return False

    return True


# ---------------------------------------------------------------------------
# Policy eligibility
# ---------------------------------------------------------------------------


class PolicyEligibility(BaseModel):
    """Result of evaluating a single Skill policy for conclusion readiness."""

    cause_code: str
    skill_name: str
    eligible: bool
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    rejection_reason: str = ""


def evaluate_policy_eligibility(
    policy: EvidencePolicy,
    material_evidence: list[Evidence],
) -> PolicyEligibility:
    """Evaluate whether a Skill policy has sufficient evidence for conclusion.

    A policy is conclusion-ready when:
      - the number of independent material source tools >= minimum_independent_evidence
      - no configured direct-contradiction source is present

    Returns a PolicyEligibility with the supporting evidence IDs.
    """
    # Group evidence by source_tool
    evidence_by_tool: dict[str, list[Evidence]] = {}
    for ev in material_evidence:
        tool = ev.source_tool
        evidence_by_tool.setdefault(tool, []).append(ev)

    # Count independent source tools
    independent_tools = set(evidence_by_tool.keys())

    # Check minimum independent evidence
    if len(independent_tools) < policy.minimum_independent_evidence:
        return PolicyEligibility(
            cause_code=policy.cause_code,
            skill_name=policy.skill_name,
            eligible=False,
            rejection_reason="insufficient_independent_evidence",
        )

    # Check direct contradictions
    for contradiction_source in policy.direct_contradictions:
        # A contradiction is a source tool that provides evidence contradicting
        # the root cause hypothesis.  We check if any evidence content matches
        # the contradiction description.
        for ev in material_evidence:
            if _matches_contradiction(ev, contradiction_source):
                return PolicyEligibility(
                    cause_code=policy.cause_code,
                    skill_name=policy.skill_name,
                    eligible=False,
                    rejection_reason="direct_contradiction",
                )

    # Collect supporting evidence IDs
    supporting_ids = [ev.id for ev in material_evidence]

    return PolicyEligibility(
        cause_code=policy.cause_code,
        skill_name=policy.skill_name,
        eligible=True,
        supporting_evidence_ids=supporting_ids,
    )


def _matches_contradiction(evidence: Evidence, contradiction: str) -> bool:
    """Check if evidence content matches a contradiction description.

    This performs a simple substring check on the evidence content values.
    """
    content = evidence.content
    contradiction_lower = contradiction.lower()

    for value in content.values():
        if isinstance(value, str) and contradiction_lower in value.lower():
            return True
        if isinstance(value, dict):
            for v in value.values():
                if isinstance(v, str) and contradiction_lower in v.lower():
                    return True
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and contradiction_lower in item.lower():
                    return True

    return False


# ---------------------------------------------------------------------------
# Conclusion readiness evaluation
# ---------------------------------------------------------------------------


class ConclusionReadiness(BaseModel):
    """Result of evaluating conclusion readiness across all loaded policies."""

    ready: bool
    eligible_cause_codes: list[str] = Field(default_factory=list)
    eligible_evidence_ids: list[str] = Field(default_factory=list)
    rejections: list[PolicyEligibility] = Field(default_factory=list)


def evaluate_conclusion_readiness(
    loaded_skill_names: list[str],
    policies_by_cause_code: dict[str, EvidencePolicy],
    evidence: list[Evidence],
    incident_id: str,
) -> ConclusionReadiness:
    """Evaluate whether any loaded Skill policy is ready for conclusion.

    Returns a ConclusionReadiness with eligible cause codes and supporting
    evidence IDs.  Does NOT choose the final cause or construct a proposal.
    """
    # Filter to material evidence for this incident
    material = [
        ev for ev in evidence
        if is_material_evidence(ev, incident_id)
    ]

    if not material:
        return ConclusionReadiness(ready=False)

    # Evaluate each loaded policy
    eligible_cause_codes: list[str] = []
    eligible_evidence_ids: list[str] = []
    rejections: list[PolicyEligibility] = []

    for cause_code, policy in policies_by_cause_code.items():
        # Only evaluate policies whose skill is loaded
        if policy.skill_name not in loaded_skill_names:
            continue

        eligibility = evaluate_policy_eligibility(policy, material)

        if eligibility.eligible:
            eligible_cause_codes.append(cause_code)
            eligible_evidence_ids.extend(eligibility.supporting_evidence_ids)
        else:
            rejections.append(eligibility)

    # Deduplicate evidence IDs
    eligible_evidence_ids = list(dict.fromkeys(eligible_evidence_ids))

    ready = len(eligible_cause_codes) > 0

    return ConclusionReadiness(
        ready=ready,
        eligible_cause_codes=eligible_cause_codes,
        eligible_evidence_ids=eligible_evidence_ids,
        rejections=rejections,
    )


# ---------------------------------------------------------------------------
# Conclusion context construction
# ---------------------------------------------------------------------------


def build_conclusion_context(
    *,
    incident_id: str,
    alert: dict[str, Any],
    loaded_skill_names: list[str],
    eligible_cause_codes: list[str],
    material_evidence: list[Evidence],
    eligible_evidence_ids: list[str],
) -> str:
    """Build a bounded prompt context for the conclusion-only model node.

    Contains only:
      - current incident summary
      - loaded Skill names
      - eligible cause codes
      - bounded material Evidence summaries and exact Evidence IDs
      - instruction to choose only supported current-incident evidence

    No observability tool information is included.
    """
    lines: list[str] = []

    lines.append("## Conclusion Phase")
    lines.append(f"Incident ID: {incident_id}")
    lines.append(f"Service: {alert.get('service', 'unknown')}")
    lines.append(f"Symptom: {alert.get('symptom', 'unknown')}")

    if loaded_skill_names:
        lines.append(f"Loaded Skills: {', '.join(loaded_skill_names)}")

    if eligible_cause_codes:
        lines.append(f"Eligible cause codes: {', '.join(eligible_cause_codes)}")

    # Evidence summaries (bounded to 12)
    lines.append("Material Evidence:")
    for ev in material_evidence[:12]:
        content_summary = _summarize_content(ev.content)
        lines.append(f"  - {ev.id}: {ev.source_tool} -> {content_summary}")

    if eligible_evidence_ids:
        lines.append(f"Eligible Evidence IDs: {', '.join(eligible_evidence_ids)}")

    lines.append("")
    lines.append(
        "Choose exactly ONE cause code from the eligible set. "
        "Cite only current-incident Evidence IDs from the eligible set. "
        "Do not call any observability tools."
    )

    return "\n".join(lines)


def _summarize_content(content: dict[str, Any]) -> str:
    """Return a short summary of evidence content."""
    if content.get("error"):
        return f"error: {str(content['error'])[:80]}"
    if content.get("empty") or content.get("empty_result"):
        return "empty"
    count = content.get("count")
    if count is not None:
        return f"{count} items"
    data = content.get("data")
    if data is not None:
        return f"data present ({type(data).__name__})"
    return "content present"


# ---------------------------------------------------------------------------
# Proposal parsing and validation
# ---------------------------------------------------------------------------


class ProposalResult(BaseModel):
    """Result of parsing a model output into a RootCauseProposal."""

    success: bool
    proposal: RootCauseProposal | None = None
    error_code: str = ""
    error_detail: str = ""


def parse_conclusion_output(
    raw_output: Any,
    eligible_cause_codes: list[str],
    eligible_evidence_ids: list[str],
) -> ProposalResult:
    """Parse and validate the model's conclusion output.

    Validates:
      - exactly one RootCauseProposal tool call
      - valid Pydantic schema
      - cause_code in eligible set
      - evidence_ids all in eligible set

    Returns a ProposalResult with success=True and the parsed proposal,
    or success=False with an error_code for repair classification.
    """
    # Extract the RootCauseProposal from the output
    proposal = _extract_proposal(raw_output)

    if proposal is None:
        return ProposalResult(
            success=False,
            error_code="no_proposal_tool_call",
            error_detail="Model did not emit a RootCauseProposal tool call",
        )

    # Validate cause_code against eligible set
    if eligible_cause_codes and proposal.cause_code not in eligible_cause_codes:
        return ProposalResult(
            success=False,
            error_code="unknown_cause_code",
            error_detail=(
                f"Cause code '{proposal.cause_code}' is not in the eligible set: "
                f"{eligible_cause_codes}"
            ),
        )

    # Validate evidence_ids against eligible set
    if eligible_evidence_ids:
        ineligible = [
            eid for eid in proposal.evidence_ids
            if eid not in eligible_evidence_ids
        ]
        if ineligible:
            return ProposalResult(
                success=False,
                error_code="unknown_evidence_id",
                error_detail=(
                    f"Evidence IDs not in eligible set: {ineligible}. "
                    f"Eligible: {eligible_evidence_ids}"
                ),
            )

    # Validate next_action literal
    if proposal.next_action not in ("finish", "needs_more_evidence"):
        return ProposalResult(
            success=False,
            error_code="invalid_next_action",
            error_detail=(
                f"next_action must be 'finish' or 'needs_more_evidence', "
                f"got '{proposal.next_action}'"
            ),
        )

    return ProposalResult(success=True, proposal=proposal)


def _extract_proposal(raw_output: Any) -> RootCauseProposal | None:
    """Extract a RootCauseProposal from various output formats."""
    if isinstance(raw_output, RootCauseProposal):
        return raw_output

    if isinstance(raw_output, dict):
        # Try direct dict parsing
        try:
            return RootCauseProposal.model_validate(raw_output)
        except (ValidationError, KeyError):
            pass

        # Try from tool_calls
        tool_calls = raw_output.get("tool_calls", [])
        for tc in tool_calls:
            if isinstance(tc, dict) and tc.get("name") == "root_cause_proposal":
                try:
                    return RootCauseProposal.model_validate(tc.get("args", {}))
                except (ValidationError, KeyError):
                    continue

    if isinstance(raw_output, list):
        for item in raw_output:
            result = _extract_proposal(item)
            if result is not None:
                return result

    return None


# ---------------------------------------------------------------------------
# Repair classification
# ---------------------------------------------------------------------------


class RepairClassification(BaseModel):
    """Classification of a proposal failure as repairable or terminal."""

    repairable: bool
    error_code: str
    repair_prompt: str = ""


def classify_repair(
    proposal_result: ProposalResult,
    eligible_cause_codes: list[str],
    eligible_evidence_ids: list[str],
) -> RepairClassification:
    """Classify a proposal failure and build a repair prompt if repairable.

    Repairable conditions (one bounded repair allowed):
      - malformed structured output (no_proposal_tool_call)
      - unknown Evidence IDs
      - unknown cause code
      - invalid next_action

    Non-repairable (terminal):
      - direct contradiction (should not reach here — caught by readiness)
      - missing Skill (should not reach here — caught by readiness)
    """
    if proposal_result.success:
        return RepairClassification(
            repairable=False,
            error_code="",
        )

    error_code = proposal_result.error_code

    # All structured output errors are repairable (one attempt)
    if error_code in (
        "no_proposal_tool_call",
        "unknown_evidence_id",
        "unknown_cause_code",
        "invalid_next_action",
    ):
        repair_parts = [
            f"Previous attempt failed: {error_code}.",
            proposal_result.error_detail,
            "",
        ]

        if eligible_cause_codes:
            repair_parts.append(
                f"Eligible cause codes: {', '.join(eligible_cause_codes)}"
            )
        if eligible_evidence_ids:
            repair_parts.append(
                f"Eligible evidence IDs: {', '.join(eligible_evidence_ids)}"
            )

        repair_parts.append(
            "Emit a valid RootCauseProposal with exactly one tool call."
        )

        return RepairClassification(
            repairable=True,
            error_code=error_code,
            repair_prompt="\n".join(repair_parts),
        )

    # Unknown errors are terminal
    return RepairClassification(
        repairable=False,
        error_code=error_code,
    )
