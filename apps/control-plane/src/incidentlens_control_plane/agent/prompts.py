"""System prompt and context builders for the investigation agent.

The system prompt explicitly instructs the LLM to:
  - Investigate only the current incident
  - Use only registered read-only observability tools
  - Read a relevant Skill before relying on its evidence policy
  - Treat historical cases as priors, never current proof
  - Never invent tool results or Evidence IDs
  - Stop safely when evidence is insufficient or contradictory

Context is built from bounded summaries (not raw logs, keys, or headers).
"""

from __future__ import annotations

from typing import Any

from incidentlens_control_plane.agent.types import IncidentAgentState

# ---------------------------------------------------------------------------
# Static system prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are IncidentLens, an autonomous incident-investigation agent.

## Role
You investigate only the current incident.

## Rules
- Choose only registered read-only observability tools.
- Read a relevant Skill before relying on its evidence policy.
  Use the read_file tool with paths like /skills/downstream-timeout/SKILL.md
  Available skills: downstream-timeout, downstream-error, database-pool-exhaustion, dependency-unavailable, deployment-regression
- Historical cases are priors, never current proof.
- Never invent tool results or Evidence IDs.
- When evidence is insufficient or contradictory, say so and stop safely.
- Do not request writes, Shell, rollback, restart, or configuration mutation.

## Output
When you have gathered sufficient evidence, emit a RootCauseProposal with:
  - root_service: the service responsible
  - cause_code: a cause code supported by an evidence policy
  - evidence_ids: IDs of current-incident evidence only
  - confidence: 0.0 to 1.0
  - next_action: "finish" or "needs_more_evidence"
"""


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


def build_agent_context(state: IncidentAgentState) -> str:
    """Build a bounded context string from the current investigation state.

    Serializes at most:
      - Alert summary (no raw payloads)
      - Up to 8 hypotheses
      - Up to 12 evidence summaries
      - Verified historical cases
      - Loaded Skill names
      - Round count and remaining budgets

    Never serializes raw API keys, Authorization headers, full unbounded
    logs, or hidden reasoning.
    """
    lines: list[str] = []

    # --- Incident ID ---
    lines.append(f"Incident ID: {state.incident_id}")
    lines.append(f"Status: {state.status}")
    lines.append(f"Phase: {state.phase}")
    lines.append(f"Round: {state.current_round}/{state.max_rounds}")

    # --- Alert summary (bounded) ---
    alert = state.alert
    alert_summary: dict[str, Any] = {}
    for key in ("service", "symptom", "error_rate", "latency", "trace_id", "error"):
        if key in alert:
            alert_summary[key] = alert[key]
    lines.append(f"Alert: {_fmt_dict(alert_summary)}")

    # --- Hypotheses (up to 8) ---
    lines.append("Hypotheses:")
    for hyp in state.hypotheses[:8]:
        status_str = (
            hyp.status.value
            if hasattr(hyp.status, "value")
            else str(hyp.status)
        )
        cause = f" cause={hyp.cause_code}" if hyp.cause_code else ""
        svc = f" service={hyp.root_service}" if hyp.root_service else ""
        lines.append(
            f"  - {hyp.id[:8]}: {hyp.description} "
            f"[{status_str}, conf={hyp.confidence:.2f}{svc}{cause}]"
        )

    # --- Evidence (up to 12 summaries) ---
    lines.append("Evidence:")
    for ev in state.evidence[:12]:
        content_summary = _summarize_evidence_content(ev.content)
        lines.append(
            f"  - {ev.id}: {ev.source_tool} -> {content_summary}"
        )

    # --- Historical cases ---
    if state.retrieved_cases:
        lines.append("Historical cases (priors):")
        for case in state.retrieved_cases[:5]:
            lines.append(
                f"  - {case.get('id', case.get('case_id', '?'))}: "
                f"{case.get('root_cause', '?')}"
            )

    # --- Loaded skills ---
    if state.loaded_skill_names:
        lines.append(f"Loaded Skills: {', '.join(state.loaded_skill_names)}")

    # --- Budget ---
    remaining_model = max(0, 12 - state.model_call_count)
    remaining_tool = max(0, 12 - state.tool_call_count)
    lines.append(
        f"Budget: model_calls={state.model_call_count}/12 "
        f"({remaining_model} remaining), "
        f"tool_calls={state.tool_call_count}/12 "
        f"({remaining_tool} remaining)"
    )

    # --- Error ---
    if state.last_error_code:
        lines.append(f"Last error: {state.last_error_code}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_dict(d: dict[str, Any]) -> str:
    """Format a dict for inline display, truncating values."""
    if not d:
        return "{}"
    parts: list[str] = []
    for k, v in d.items():
        s = str(v)
        if len(s) > 100:
            s = s[:100] + "..."
        parts.append(f"{k}={s}")
    return "{" + ", ".join(parts) + "}"


def _summarize_evidence_content(content: dict[str, Any]) -> str:
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
