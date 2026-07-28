"""Agent middleware for audit logging, budget enforcement, and report gating.

Middleware classes implement LangChain's AgentMiddleware protocol:

  - AuditMiddleware: records every model call and tool call to the audit store
  - EvidenceRecordingMiddleware: converts ToolMessage artifacts into state updates
    (evidence + tool_call_count), creating invalid_arguments Evidence on validation errors
  - BudgetEnforcementMiddleware: enforces model and tool call limits
  - ReportGateMiddleware: gates the final structured output through evidence policies
"""

from __future__ import annotations

import json
from typing import Any

from incidentlens_contracts.models import Evidence
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    Runtime,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command
from pydantic import ValidationError

from incidentlens_control_plane.agent.state import InvestigationAuditStore
from incidentlens_control_plane.agent.tool_adapter import AgentToolEnvelope
from incidentlens_control_plane.agent.types import IncidentAgentState, RootCauseProposal
from incidentlens_control_plane.agent.skills import SkillRuntime


# ---------------------------------------------------------------------------
# Audit middleware
# ---------------------------------------------------------------------------


class AuditMiddleware(AgentMiddleware[IncidentAgentState, Any]):
    """Records model calls and tool calls to the audit store."""

    name = "AuditMiddleware"

    def __init__(self, audit_store: InvestigationAuditStore) -> None:
        self._audit_store = audit_store

    async def abefore_model(
        self,
        state: IncidentAgentState,
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        """Record a model call audit entry before the model executes."""
        incident_id = state.get("incident_id", "unknown")
        self._audit_store.record(
            incident_id,
            "model_call",
            {
                "round": state.get("current_round", 0),
                "model_call_count": state.get("model_call_count", 0),
            },
        )
        return {"model_call_count": 1}

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Any,
    ) -> ToolMessage | Command[Any]:
        """Record tool call audit entry and pass through to handler."""
        state = request.state
        incident_id = state.get("incident_id", "unknown") if isinstance(state, dict) else "unknown"
        tool_call = request.tool_call

        self._audit_store.record(
            incident_id,
            "tool_call",
            {
                "tool": tool_call.get("name", ""),
                "args": _safe_tool_args(tool_call.get("args", {})),
                "call_id": tool_call.get("id", ""),
            },
        )

        return await handler(request)


def _safe_tool_args(args: Any) -> dict[str, Any]:
    """Serialize tool args to a JSON-safe dict, truncating large values."""
    if not isinstance(args, dict):
        return {"value": str(args)[:200]}
    safe: dict[str, Any] = {}
    for k, v in args.items():
        s = str(v)
        if len(s) > 200:
            s = s[:200] + "..."
        safe[k] = s
    return safe


# ---------------------------------------------------------------------------
# Evidence recording middleware
# ---------------------------------------------------------------------------


class EvidenceRecordingMiddleware(AgentMiddleware[IncidentAgentState, Any]):
    """Converts ToolMessage artifacts into LangGraph state updates.

    After each tool call, extracts the AgentToolEnvelope from the tool result's
    artifact and appends the evidence to the agent state. On Pydantic validation
    errors, creates a synthetic 'invalid_arguments' evidence record and returns
    an error ToolMessage through Command, permitting one model repair.
    """

    name = "EvidenceRecordingMiddleware"

    def __init__(self) -> None:
        self._consecutive_invalid: int = 0

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Any,
    ) -> ToolMessage | Command[Any]:
        """Intercept tool calls to record evidence from artifacts."""
        tool_call = request.tool_call
        tool = request.tool
        state = request.state

        try:
            response = await handler(request)
        except ValidationError as exc:
            # Validation error from the tool's Pydantic args
            self._consecutive_invalid += 1
            if self._consecutive_invalid >= 2:
                # Two consecutive invalid calls: stop repairing
                error_msg = f"Tool arguments invalid twice: {exc}"
                return ToolMessage(
                    content=error_msg,
                    tool_call_id=tool_call.get("id", ""),
                    status="error",
                )

            # Build synthetic invalid_arguments evidence
            incident_id = (
                state.get("incident_id", "unknown")
                if isinstance(state, dict)
                else "unknown"
            )
            synthetic_evidence = Evidence(
                id=f"ev-invalid-{tool_call.get('id', 'unknown')[:16]}",
                source_tool=tool_call.get("name", "unknown"),
                tool_call_id=tool_call.get("id", ""),
                content={
                    "incident_id": incident_id,
                    "outcome": "invalid_arguments",
                    "error": str(exc),
                    "tool_call": _safe_tool_args(tool_call.get("args", {})),
                },
            )

            error_msg = f"Invalid tool arguments (repair allowed): {exc}"
            tool_msg = ToolMessage(
                content=error_msg,
                tool_call_id=tool_call.get("id", ""),
                status="error",
            )
            return Command(
                update={
                    "messages": [tool_msg],
                    "evidence": [synthetic_evidence],
                    "tool_call_count": 1,
                },
            )

        # Success path: reset consecutive invalid count and extract evidence
        self._consecutive_invalid = 0

        if isinstance(response, ToolMessage) and hasattr(response, "artifact") and response.artifact is not None:
            try:
                envelope = AgentToolEnvelope.model_validate(response.artifact)
                return Command(
                    update={
                        "messages": [response],
                        "evidence": [envelope.evidence],
                        "tool_call_count": 1,
                    },
                )
            except Exception:
                # If artifact can't be parsed, pass through without state update
                return response

        return response


# ---------------------------------------------------------------------------
# Budget enforcement middleware
# ---------------------------------------------------------------------------


class BudgetEnforcementMiddleware(AgentMiddleware[IncidentAgentState, Any]):
    """Enforces model and tool call limits.

    When either limit is exceeded, returns an AIMessage instructing the agent
    to finalize with 'budget_exhausted' and sets last_error_code on the state.
    """

    name = "BudgetEnforcementMiddleware"

    def __init__(
        self,
        model_limit: int = 12,
        tool_limit: int = 12,
    ) -> None:
        self._model_limit = model_limit
        self._tool_limit = tool_limit

    async def abefore_model(
        self,
        state: IncidentAgentState,
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        """Check if budget is exhausted before allowing another model call."""
        model_calls = state.get("model_call_count", 0)
        tool_calls = state.get("tool_call_count", 0)

        if model_calls >= self._model_limit:
            return {
                "last_error_code": "budget_exhausted",
                "status": "needs_more_evidence",
            }

        if tool_calls >= self._tool_limit:
            return {
                "last_error_code": "budget_exhausted",
                "status": "needs_more_evidence",
            }

        return None

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Any,
    ) -> ModelResponse | AIMessage:
        """Check budget before model call; short-circuit if exhausted."""
        state = request.state
        model_calls = state.get("model_call_count", 0)
        tool_calls = state.get("tool_call_count", 0)

        if model_calls >= self._model_limit or tool_calls >= self._tool_limit:
            # Short-circuit: return a final message instead of calling the model
            return ModelResponse(
                result=[
                    AIMessage(
                        content=(
                            "Budget exhausted. "
                            "I cannot continue investigating. "
                            "Please review the evidence collected so far."
                        ),
                    )
                ],
                structured_response=None,
            )

        return await handler(request)


# ---------------------------------------------------------------------------
# Report gate middleware
# ---------------------------------------------------------------------------


class ReportGateMiddleware(AgentMiddleware[IncidentAgentState, Any]):
    """Gates the structured output (RootCauseProposal) through evidence policies.

    When the model emits a RootCauseProposal, this middleware validates:
      1. All evidence_ids reference known evidence from the current incident
      2. The cause_code matches a known evidence policy
      3. Sufficient independent evidence types exist
      4. No direct contradictions
    """

    name = "ReportGateMiddleware"

    def __init__(self, skill_runtime: SkillRuntime) -> None:
        self._skill_runtime = skill_runtime

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Any,
    ) -> ModelResponse | AIMessage:
        """Intercept the model call to gate the structured response."""
        response = await handler(request)

        if response.structured_response is None:
            return response

        proposal = response.structured_response
        state = request.state

        # Validate the proposal
        if isinstance(proposal, RootCauseProposal):
            decision = can_generate_guarded_report(
                state,
                proposal,
                self._skill_runtime.policies_by_cause_code,
            )
            if not decision.allowed:
                # Replace structured response with None and return feedback
                return ModelResponse(
                    result=[
                        AIMessage(
                            content=(
                                f"Report rejected: {decision.reason}. "
                                "Please continue investigating."
                            ),
                        )
                    ],
                    structured_response=None,
                )

        return response


# ---------------------------------------------------------------------------
# Guard decision (public API)
# ---------------------------------------------------------------------------


class GuardDecision:
    """Result of the report gate check."""

    def __init__(self, allowed: bool, reason: str = "") -> None:
        self.allowed = allowed
        self.reason = reason

    def __repr__(self) -> str:
        return f"GuardDecision(allowed={self.allowed!r}, reason={self.reason!r})"


def can_generate_guarded_report(
    state: IncidentAgentState | dict[str, Any],
    proposal: RootCauseProposal,
    policies_by_cause_code: dict[str, Any] | None = None,
) -> GuardDecision:
    """Determine whether a RootCauseProposal may pass the report gate.

    Checks:
      1. All evidence_ids must exist in the current incident's evidence
      2. The cause_code must be known (if policies are provided)
      3. Evidence must come from independent source tools
      4. No direct contradictions in the evidence
    """
    # Get owned evidence IDs
    evidence = state.get("evidence", []) if isinstance(state, dict) else getattr(state, "evidence", [])
    owned_ids = set()
    for ev in evidence:
        if isinstance(ev, dict):
            owned_ids.add(ev.get("id", ""))
        else:
            owned_ids.add(ev.id)

    # Check: all evidence_ids must be current-incident evidence
    if not proposal.evidence_ids:
        return GuardDecision(
            allowed=False,
            reason="current_incident_evidence_required",
        )

    for ev_id in proposal.evidence_ids:
        if ev_id not in owned_ids:
            return GuardDecision(
                allowed=False,
                reason="unknown_evidence_id",
            )

    # Check: cause_code must be known
    if policies_by_cause_code and proposal.cause_code not in policies_by_cause_code:
        return GuardDecision(
            allowed=False,
            reason="unknown_cause_code",
        )

    # Check: independent source types
    evidence_by_id = {}
    for ev in evidence:
        if isinstance(ev, dict):
            evidence_by_id[ev.get("id", "")] = ev
        else:
            evidence_by_id[ev.id] = ev

    source_tools = set()
    for ev_id in proposal.evidence_ids:
        ev = evidence_by_id.get(ev_id)
        if ev:
            tool = ev.get("source_tool", "") if isinstance(ev, dict) else ev.source_tool
            source_tools.add(tool)

    if policies_by_cause_code and proposal.cause_code in policies_by_cause_code:
        policy = policies_by_cause_code[proposal.cause_code]
        min_independent = getattr(policy, "minimum_independent_evidence", 0)
        if len(source_tools) < min_independent:
            return GuardDecision(
                allowed=False,
                reason="insufficient_independent_evidence",
            )

        # Check direct contradictions
        direct_contradictions = getattr(policy, "direct_contradictions", [])
        for ev_id in proposal.evidence_ids:
            ev = evidence_by_id.get(ev_id)
            if ev:
                tool = ev.get("source_tool", "") if isinstance(ev, dict) else ev.source_tool
                if tool in direct_contradictions:
                    return GuardDecision(
                        allowed=False,
                        reason="direct_contradiction",
                    )

    return GuardDecision(allowed=True, reason="")


# ---------------------------------------------------------------------------
# Secret redaction for SSE / audit safety
# ---------------------------------------------------------------------------

_REDACTED = "**REDACTED**"

_SENSITIVE_KEY_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "api_key",
        "token",
        "secret",
        "api-key",
        "x-api-key",
        "password",
        "credential",
    }
)


def redact_sensitive_payload(
    payload: dict[str, Any],
    *,
    secret_values: set[str] | None = None,
) -> dict[str, Any]:
    """Return a deep copy of *payload* with sensitive values replaced.

    A key is considered sensitive when its lowercased name matches one of the
    well-known patterns (``api_key``, ``authorization``, ``token``, etc.) **or**
    when its value appears in the caller-supplied *secret_values* set.

    The redaction is shallow on purpose: nested dicts are recursed into, but
    lists are iterated element-wise only if they contain dicts.  Primitive
    list elements that match a secret value are replaced by ``"**REDACTED**"``.
    """
    secrets = set(secret_values or set())
    return _redact_node(payload, secrets)


def _redact_node(node: Any, secrets: set[str]) -> Any:
    """Recursively redact a nested structure."""
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for k, v in node.items():
            # Redact value if the key is a known sensitive name
            if _is_sensitive_key(k):
                result[k] = _REDACTED
            else:
                result[k] = _redact_value(v, secrets)
        return result
    if isinstance(node, list):
        return [_redact_value(item, secrets) for item in node]
    if isinstance(node, str) and node in secrets:
        return _REDACTED
    return node


def _redact_value(value: Any, secrets: set[str]) -> Any:
    """Redact a value if it contains a secret."""
    if isinstance(value, str):
        if value in secrets:
            return _REDACTED
        # Strip values that contain secret substrings
        for secret in secrets:
            if secret in value:
                return _REDACTED
    if isinstance(value, dict):
        return _redact_node(value, secrets)
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    """Return True if the key name matches well-known sensitive patterns."""
    return key.lower() in _SENSITIVE_KEY_NAMES


# ---------------------------------------------------------------------------
# Policy access helper
# ---------------------------------------------------------------------------


class _PoliciesByCauseCode:
    """Adapter to expose SkillRuntime.policies_by_cause_code as a dict."""

    def __init__(self, skill_runtime: SkillRuntime) -> None:
        self._skill_runtime = skill_runtime

    @property
    def _map(self) -> dict[str, Any]:
        if self._skill_runtime._definitions is None:
            self._skill_runtime.validate()
        return self._skill_runtime._cause_code_map

    def __getitem__(self, key: str) -> Any:
        return self._map[key]

    def __contains__(self, key: str) -> bool:
        return key in self._map

    def get(self, key: str, default: Any = None) -> Any:
        return self._map.get(key, default)
