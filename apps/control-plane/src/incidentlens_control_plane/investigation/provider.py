"""Provider-neutral model contract for the Phase 4 bounded investigation loop.

The orchestrator never talks to a specific LLM or vendor SDK: it calls
``ModelProvider.generate_turn(ConversationRequest)`` and receives an
``AgentTurnResult`` of *proposals*.  A provider may only propose tool
requests, a child delegation, hypotheses, a conclusion and a stop signal — it
never executes tools, never writes the store and never observes FastAPI or
SQLite.  Everything the provider can see is spelled out in
``ConversationRequest``: the run checkpoint, the investigation snapshot, the
task prompt and the append-only transcript ``messages`` it must continue, plus
the tool schemas it is allowed to call.  There is deliberately no field for
raw hidden reasoning, and every model is ``extra="forbid"`` so a provider
cannot smuggle either into a turn.

``ProviderOutputValidator`` is the orchestrator-side gatekeeper that rejects a
provider result which names an un-allowlisted tool, fails a tool's argument
JSON schema, leaves the run's scope, cites evidence the run does not own, or
declares a contradictory action.  It reuses ``InvestigationGuard`` for the
budget and evidence-ownership checks, so the tests exercise the same guard
paths the loop will use at runtime.
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from incidentlens_control_plane.investigation.guard import InvestigationGuard
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    Conclusion,
    Hypothesis,
    HypothesisStatus,
    InvestigationBudget,
    ProviderUsage,
    StopReason,
    TranscriptMessage,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope

# Provider-declarable stop reasons.  The BUDGET_* reasons are detected by the
# guard and orchestrator, never declared by the model, so a provider that names
# one is rejected.
_PROVIDER_DECLARABLE_STOPS: frozenset[StopReason] = frozenset(
    {
        StopReason.COMPLETED,
        StopReason.MISSING_EVIDENCE,
        StopReason.PENDING_APPROVAL,
        StopReason.UNCERTAIN_STATE,
        StopReason.CANCELLED,
        StopReason.FAILED,
    }
)

# Placeholder timestamp used only when validating a hypothesis proposal through
# the domain guard; the orchestrator assigns real identity/timestamps later.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ProviderError(Exception):
    """A recoverable provider failure (rate limit, transport blip, ...).

    ``retryable`` lets the orchestrator decide whether the run may retry the
    turn or must pause; the default is to retry.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class ProviderCrash(Exception):
    """A non-retryable provider failure — the run must fail, not retry."""


class ProviderContextMismatch(Exception):
    """Raised when a ``ConversationRequest`` and its ``AgentRun`` disagree on identity.

    The validator derives the tool allowlist from the request but the budget,
    scope and evidence-ownership checks from the run, so handing it a
    mismatched pair would silently disable those checks.  The orchestrator must
    always pass the run that produced the request.
    """


class ProviderOutputRejected(Exception):
    """Raised when a provider result fails schema/allowlist/scope/ownership checks."""


def _validate_unique_ids(
    cls: type[object], value: tuple[str, ...]
) -> tuple[str, ...]:
    """Reject empty-string or duplicate id tuples in provider proposals."""
    if any(not item.strip() for item in value):
        raise ValueError("ids must not contain empty strings")
    if len(value) != len(set(value)):
        raise ValueError("ids must be unique")
    return value


def _json_compatible(value: Any) -> bool:
    """Return True when ``value`` is a JSON-serializable plain structure."""
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_compatible(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _json_compatible(item)
            for key, item in value.items()
        )
    return False


def _validate_schema(
    value: Any, schema: dict[str, Any], path: str = "arguments"
) -> tuple[bool, str]:
    """Validate ``value`` against a compact JSON-Schema subset.

    Covers the keywords the tool schemas in this repository use: ``type``
    (object/array/string/integer/number/boolean/null), ``properties``,
    ``required``, ``additionalProperties``, ``items``, ``enum``, ``minLength``,
    ``maxLength``, ``pattern``, ``minimum``, ``maximum``, ``minItems`` and
    ``maxItems``.  Unknown keywords are ignored, mirroring JSON Schema.
    """
    kind = schema.get("type")
    if kind is None:
        return True, "ok"
    if "enum" in schema and value not in schema["enum"]:
        return False, f"{path} must be one of {schema['enum']!r}"
    if kind == "object":
        if not isinstance(value, dict):
            return False, f"{path} must be an object"
        properties = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                return False, f"{path} is missing required property {name!r}"
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                return False, f"{path} has unknown properties {sorted(unknown)!r}"
        for name, property_schema in properties.items():
            if name in value:
                ok, msg = _validate_schema(value[name], property_schema, f"{path}.{name}")
                if not ok:
                    return False, msg
        return True, "ok"
    if kind == "array":
        if not isinstance(value, list):
            return False, f"{path} must be an array"
        if len(value) < schema.get("minItems", 0):
            return False, f"{path} has fewer than minItems items"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return False, f"{path} has more than maxItems items"
        items = schema.get("items")
        if items is not None:
            for index, item in enumerate(value):
                ok, msg = _validate_schema(item, items, f"{path}[{index}]")
                if not ok:
                    return False, msg
        return True, "ok"
    if kind == "string":
        if not isinstance(value, str):
            return False, f"{path} must be a string"
        if len(value) < schema.get("minLength", 0):
            return False, f"{path} is shorter than minLength"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return False, f"{path} is longer than maxLength"
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return False, f"{path} does not match {schema['pattern']!r}"
        return True, "ok"
    if kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return False, f"{path} must be an integer"
        if "minimum" in schema and value < schema["minimum"]:
            return False, f"{path} is below minimum"
        if "maximum" in schema and value > schema["maximum"]:
            return False, f"{path} is above maximum"
        return True, "ok"
    if kind == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False, f"{path} must be a number"
        if "minimum" in schema and value < schema["minimum"]:
            return False, f"{path} is below minimum"
        if "maximum" in schema and value > schema["maximum"]:
            return False, f"{path} is above maximum"
        return True, "ok"
    if kind == "boolean":
        if not isinstance(value, bool):
            return False, f"{path} must be a boolean"
        return True, "ok"
    if kind == "null":
        if value is not None:
            return False, f"{path} must be null"
        return True, "ok"
    return True, "ok"


def _paths_subset(
    child: tuple[PurePosixPath, ...], parent: tuple[PurePosixPath, ...]
) -> bool:
    """Return True when every child path is contained by a parent root.

    An empty parent path set is unbounded.  A bounded parent requires at least
    one child root, and every child root must be contained by a parent root.
    """
    if not parent:
        return True
    return bool(child) and all(
        any(child_path.is_relative_to(parent_root) for parent_root in parent)
        for child_path in child
    )


def _scope_within(child: AgentScope, parent: AgentScope) -> tuple[bool, str]:
    """Return True when a child scope is a legal narrowing of the run scope."""
    if child.project_id != parent.project_id:
        return False, "child scope project must match the run"
    if child.target_id != parent.target_id:
        return False, "child scope target must match the run"
    if parent.scope is LogScope.CONTAINER:
        if child.scope is not LogScope.CONTAINER:
            return False, "a container run may only delegate container-scoped children"
        if child.service_name != parent.service_name:
            return False, "child scope service must match the run"
        if child.container_name != parent.container_name:
            return False, "child scope container must match the run"
        if not _paths_subset(child.allowed_container_paths, parent.allowed_container_paths):
            return False, "child container paths must be within the run's allowed paths"
    elif child.scope is LogScope.HOST:
        if not _paths_subset(child.allowed_host_paths, parent.allowed_host_paths):
            return False, "child host paths must be within the run's allowed paths"
    # A HOST parent may delegate a CONTAINER child: this is the allowed
    # narrowing, and the child's container paths live in their own namespace,
    # already bounded by AgentScope validation.
    return True, "child scope is within the run scope"


def _proposal_to_hypothesis(run: AgentRun, proposal: HypothesisProposal) -> Hypothesis:
    """Build a throwaway domain Hypothesis so the guard can validate citations."""
    return Hypothesis(
        hypothesis_id="__validator__",
        agent_run_id=run.agent_run_id,
        summary=proposal.summary,
        facts=proposal.facts,
        inferences=proposal.inferences,
        unknowns=proposal.unknowns,
        evidence_ids=proposal.evidence_ids,
        status=HypothesisStatus.PROPOSED,
        created_at=_EPOCH,
        updated_at=_EPOCH,
    )


class ToolSchema(BaseModel):
    """The description of one tool a run may call, as the provider sees it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2_000)
    parameters_json_schema: dict[str, Any]
    allowed_scope: LogScope | None = Field(
        default=None,
        description="When set, the tool may only be called by a run of this scope.",
    )
    output_cap_bytes: int = Field(
        default=512 * 1024,
        ge=1,
        description="Advisory cap the provider should respect when sizing tool "
        "arguments.  The run budget (``AgentBudget.max_output_bytes_per_tool``) "
        "is the binding per-tool output limit enforced by the guard; this value "
        "is guidance only and is not independently enforced.",
    )
    requires_approval: bool = False
    concurrency_safe: bool = Field(
        default=False,
        description="When True, the tool only reads remote state or atomically "
        "replaces the run's own plan, so a batched run may execute it alongside "
        "other concurrency-safe calls. Mutations, approval-gated and control "
        "tools stay serial.",
    )

    @field_validator("parameters_json_schema")
    @classmethod
    def _parameters_must_be_object_schema(
        cls, value: dict[str, Any]
    ) -> dict[str, Any]:
        if value.get("type") not in (None, "object"):
            raise ValueError("tool parameters schema must describe an object")
        return value


class ToolRequest(BaseModel):
    """A tool invocation the provider proposes; never executed by the provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: str = Field(min_length=1, max_length=120)
    provider_tool_call_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        exclude=True,
        description="Harness-only correlation; providers must leave this unset.",
    )
    tool_name: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def _arguments_must_be_json_compatible(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not _json_compatible(value):
            raise ValueError("tool arguments must be JSON-compatible plain values")
        return value


class HypothesisProposal(BaseModel):
    """The provider's proposed hypothesis; the orchestrator assigns identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=4_000)
    facts: tuple[str, ...] = Field(default=(), max_length=32)
    inferences: tuple[str, ...] = Field(default=(), max_length=32)
    unknowns: tuple[str, ...] = Field(default=(), max_length=32)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=24)

    _validate_evidence_ids = field_validator("evidence_ids")(_validate_unique_ids)


class ChildDelegationRequest(BaseModel):
    """The provider's proposal to spawn an independent child run.

    The child scope must be a legal narrowing of the parent run's scope, and
    any seed evidence must already belong to the parent run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    child_run_id: str = Field(min_length=1, max_length=120)
    task_prompt: str = Field(min_length=1, max_length=4_000)
    scope: AgentScope
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=24)

    _validate_evidence_ids = field_validator("evidence_ids")(_validate_unique_ids)


class StopSignal(BaseModel):
    """A provider-declared request to stop the run for a stated reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stop_reason: StopReason
    summary: str = Field(min_length=1, max_length=4_000)


class RunCheckpoint(BaseModel):
    """The structured checkpoint the provider sees — not the full run.

    Carries the run identity, kind, status, round number, scope and budgets so
    the provider can stay in bounds, but no evidence, hypotheses or hidden
    reasoning.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_run_id: str = Field(min_length=1, max_length=120)
    kind: AgentRunKind
    status: AgentRunStatus
    round_number: int = Field(ge=0)
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=120)
    scope: AgentScope
    budget: AgentBudget
    usage: UsageCounters


class InvestigationSnapshot(BaseModel):
    """The bounded investigation summary the provider sees."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: str = Field(min_length=1, max_length=120)
    incident_id: str = Field(min_length=1, max_length=120)
    service: str = Field(min_length=1, max_length=120)
    allowed_log_paths: tuple[str, ...] = Field(default=(), max_length=64)
    symptom: str = Field(min_length=1, max_length=2_000)
    status: InvestigationStatus
    budget: InvestigationBudget
    usage: UsageCounters


class ConversationRequest(BaseModel):
    """The continuous, bounded conversation handed to a provider for one turn.

    Carries the run checkpoint and investigation snapshot for bounds, the
    optional child task prompt, and the append-only transcript ``messages`` the
    model must continue.  Every message block is a bounded text, tool_use or
    tool_result block; there is no raw hidden reasoning and no out-of-scope
    reference.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint: RunCheckpoint
    investigation: InvestigationSnapshot
    task_prompt: str | None = Field(default=None, min_length=1, max_length=4_000)
    messages: tuple[TranscriptMessage, ...]
    tool_schemas: tuple[ToolSchema, ...] = Field(default=(), max_length=32)
    memory_present: bool = Field(
        default=False,
        description="Whether the run has access to the project memory store, "
        "advertised so the provider can stay in bounds without touching the store.",
    )

    @model_validator(mode="after")
    def _tool_schemas_must_be_unique(self) -> ConversationRequest:
        names = [schema.tool_name for schema in self.tool_schemas]
        if len(names) != len(set(names)):
            raise ValueError("tool_schemas must be unique by tool_name")
        return self


class PromptTooLongError(ProviderError):
    """The model context exceeded the provider's window; the turn cannot retry."""

    def __init__(self, message: str = "model context is too long") -> None:
        super().__init__(message, retryable=False)


class ProviderOutputFormatError(ProviderError):
    """The provider returned JSON that does not satisfy ``AgentTurnResult``."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class AgentTurnResult(BaseModel):
    """What a provider may declare for one turn: proposals only, never effects."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_requests: tuple[ToolRequest, ...] = Field(default=(), max_length=8)
    hypotheses: tuple[HypothesisProposal, ...] = Field(default=(), max_length=16)
    conclusions: tuple[Conclusion, ...] = Field(default=(), max_length=8)
    child_delegation: ChildDelegationRequest | None = None
    stop_signal: StopSignal | None = None
    usage: ProviderUsage = ProviderUsage()

    @model_validator(mode="after")
    def _at_most_one_continuation(self) -> AgentTurnResult:
        """A turn continues one way: request tools, delegate a child, or stop."""
        actions = [
            bool(self.tool_requests),
            self.child_delegation is not None,
            self.stop_signal is not None,
        ]
        if sum(actions) > 1:
            raise ValueError(
                "provider may request tools, delegate a child or stop, not more than one"
            )
        if self.child_delegation is not None and self.conclusions:
            raise ValueError("a child-delegation turn must not declare conclusions")
        return self


class ProviderValidation(BaseModel):
    """The validated interpretation of a provider turn, as the loop will use it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requires_approval: bool = False


class ProviderOutputValidator:
    """Reject provider output that is un-allowlisted, out-of-scope or ungrounded.

    The validator is orchestrator-side: it sees the full ``AgentRun`` (not just
    the bounded context) so it can reuse ``InvestigationGuard`` for budget and
    evidence-ownership checks.  ``validate`` raises ``ProviderOutputRejected``
    on the first violation and otherwise returns a ``ProviderValidation``.
    """

    def __init__(
        self,
        request: ConversationRequest,
        run: AgentRun,
        *,
        guard: InvestigationGuard | None = None,
    ) -> None:
        self._check_identity(request, run)
        self._request = request
        self._run = run
        self._guard = guard or InvestigationGuard()
        self._schemas = {schema.tool_name: schema for schema in request.tool_schemas}

    @staticmethod
    def _check_identity(request: ConversationRequest, run: AgentRun) -> None:
        """Fail fast when the request and run do not describe the same run.

        The tool allowlist comes from the request while the budget, scope and
        evidence-ownership checks come from ``run``; a mismatched pair would
        silently validate output against a different run.
        """
        if request.checkpoint.agent_run_id != run.agent_run_id:
            raise ProviderContextMismatch(
                "request checkpoint names run "
                f"{request.checkpoint.agent_run_id!r} but the provided run is "
                f"{run.agent_run_id!r}"
            )
        if request.investigation.investigation_id != run.investigation_id:
            raise ProviderContextMismatch(
                "request names investigation "
                f"{request.investigation.investigation_id!r} but the provided "
                f"run belongs to {run.investigation_id!r}"
            )
        if request.checkpoint.scope != run.scope:
            raise ProviderContextMismatch(
                "request checkpoint scope does not match the provided run scope"
            )

    def validate(self, result: AgentTurnResult) -> ProviderValidation:
        """Return the validated turn, raising ``ProviderOutputRejected`` on failure."""
        self._check_output_budget(result)
        self._check_not_empty(result)
        self._check_stop_signal(result)
        requires_approval = self._check_tool_requests(result)
        self._check_hypotheses(result)
        self._check_conclusions(result)
        self._check_child_delegation(result)
        return ProviderValidation(requires_approval=requires_approval)

    # -- individual checks ----------------------------------------------------

    def _check_output_budget(self, result: AgentTurnResult) -> None:
        allowed, reason = self._guard.can_accept_output(
            self._run, result.usage.output_bytes
        )
        if not allowed:
            raise ProviderOutputRejected(f"provider output rejected: {reason}")

    def _check_not_empty(self, result: AgentTurnResult) -> None:
        if not (
            result.tool_requests
            or result.hypotheses
            or result.conclusions
            or result.child_delegation is not None
            or result.stop_signal is not None
        ):
            raise ProviderOutputRejected("provider output rejected: empty turn declares no action")

    def _check_stop_signal(self, result: AgentTurnResult) -> None:
        if result.stop_signal is None:
            return
        if result.stop_signal.stop_reason not in _PROVIDER_DECLARABLE_STOPS:
            raise ProviderOutputRejected(
                "provider output rejected: stop reason "
                f"{result.stop_signal.stop_reason.value!r} is not declarable by a provider"
            )

    def _check_tool_requests(self, result: AgentTurnResult) -> bool:
        seen: set[str] = set()
        requires_approval = False
        for request in result.tool_requests:
            if request.tool_call_id in seen:
                raise ProviderOutputRejected(
                    f"provider output rejected: duplicate tool_call_id {request.tool_call_id!r}"
                )
            seen.add(request.tool_call_id)
            schema = self._schemas.get(request.tool_name)
            if schema is None:
                raise ProviderOutputRejected(
                    f"provider output rejected: tool {request.tool_name!r} is not allowlisted"
                )
            self._check_tool_scope(request, schema)
            self._check_tool_arguments(request, schema)
            if schema.requires_approval:
                requires_approval = True
        return requires_approval

    def _check_tool_scope(self, request: ToolRequest, schema: ToolSchema) -> None:
        if schema.allowed_scope is None:
            return
        if schema.allowed_scope is not self._run.scope.scope:
            raise ProviderOutputRejected(
                "provider output rejected: tool "
                f"{request.tool_name!r} requires {schema.allowed_scope.value} scope "
                f"but the run is {self._run.scope.scope.value}"
            )

    def _check_tool_arguments(self, request: ToolRequest, schema: ToolSchema) -> None:
        ok, message = _validate_schema(
            request.arguments, schema.parameters_json_schema, "arguments"
        )
        if not ok:
            raise ProviderOutputRejected(
                f"provider output rejected: tool {request.tool_name!r} arguments invalid: "
                f"{message}"
            )

    def _check_hypotheses(self, result: AgentTurnResult) -> None:
        for proposal in result.hypotheses:
            if not proposal.evidence_ids:
                # An unproven hypothesis may be proposed before it has evidence.
                continue
            hypothesis = _proposal_to_hypothesis(self._run, proposal)
            allowed, reason = self._guard.validate_hypothesis(self._run, hypothesis)
            if not allowed:
                raise ProviderOutputRejected(f"provider output rejected: {reason}")

    def _check_conclusions(self, result: AgentTurnResult) -> None:
        for conclusion in result.conclusions:
            if not conclusion.evidence_ids:
                # Empty citations signal missing evidence; the loop maps it to a
                # MISSING_EVIDENCE pause rather than treating it as fabricated.
                continue
            allowed, reason = self._guard.validate_conclusion(self._run, conclusion)
            if not allowed:
                raise ProviderOutputRejected(f"provider output rejected: {reason}")

    def _check_child_delegation(self, result: AgentTurnResult) -> None:
        delegation = result.child_delegation
        if delegation is None:
            return
        if delegation.child_run_id == self._run.agent_run_id:
            raise ProviderOutputRejected(
                "provider output rejected: child_run_id must differ from the run"
            )
        allowed, reason = _scope_within(delegation.scope, self._run.scope)
        if not allowed:
            raise ProviderOutputRejected(f"provider output rejected: {reason}")
        owned = {reference.evidence_id for reference in self._run.evidence}
        missing = set(delegation.evidence_ids) - owned
        if missing:
            raise ProviderOutputRejected(
                "provider output rejected: child delegation cites evidence not "
                "collected in this investigation"
            )


class ModelProvider(ABC):
    """A provider-neutral model that proposes, never executes.

    Implementations must not execute tools, persist state or inspect the host
    runtime; they only translate a ``ConversationRequest`` into an
    ``AgentTurnResult`` for the orchestrator to validate and execute.
    """

    @abstractmethod
    async def generate_turn(self, request: ConversationRequest) -> AgentTurnResult:
        """Return the proposed actions for one turn, without side effects."""


__all__ = [
    "AgentTurnResult",
    "ChildDelegationRequest",
    "ConversationRequest",
    "HypothesisProposal",
    "InvestigationSnapshot",
    "ModelProvider",
    "PromptTooLongError",
    "ProviderCrash",
    "ProviderContextMismatch",
    "ProviderError",
    "ProviderOutputRejected",
    "ProviderOutputValidator",
    "ProviderValidation",
    "RunCheckpoint",
    "StopSignal",
    "ToolRequest",
    "ToolSchema",
]
