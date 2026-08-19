"""Scripted Fake Provider for tests that must walk the real validation paths.

A ``FakeProvider`` is driven by ``FakeScriptStep`` sequences registered in a
``FakeProviderRegistry`` under a run id.  Each step is addressed by a stable
run id plus an index, so a test can arrange a run's script up front, then
replay turn after turn and inspect exactly which step the provider executed.

The fake never executes tools or writes stores: it only emits the proposals
the orchestrator (or a test) validates with the real ``ProviderOutputValidator``
and ``InvestigationGuard``.  It can simulate tool requests, child delegation,
a stop signal, hypotheses, conclusions, usage, structurally malformed output
(``SchemaViolationStep``), semantically invalid output that only the validator
catches (``MalformedStep``), a recoverable ``ProviderError`` and a hard
``ProviderCrash``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.investigation.provider import (
    AgentTurnResult,
    ChildDelegationRequest,
    Conclusion,
    ConversationRequest,
    HypothesisProposal,
    ModelProvider,
    ProviderCrash,
    ProviderError,
    StopSignal,
    ToolRequest,
)
from incidentlens_control_plane.investigation.types import ProviderUsage


class _StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RequestToolsStep(_StepBase):
    """One turn that proposes tool calls, optionally with hypotheses/a conclusion."""

    kind: Literal["request_tools"] = "request_tools"
    tool_requests: tuple[ToolRequest, ...]
    hypotheses: tuple[HypothesisProposal, ...] = ()
    conclusion: Conclusion | None = None
    usage: ProviderUsage = ProviderUsage()


class DelegateChildStep(_StepBase):
    """One turn that proposes spawning an independent child run."""

    kind: Literal["delegate_child"] = "delegate_child"
    delegation: ChildDelegationRequest
    usage: ProviderUsage = ProviderUsage()


class StopStep(_StepBase):
    """One turn that declares a stop signal, optionally with findings."""

    kind: Literal["stop"] = "stop"
    stop_signal: StopSignal
    hypotheses: tuple[HypothesisProposal, ...] = ()
    conclusion: Conclusion | None = None
    usage: ProviderUsage = ProviderUsage()


class MalformedStep(_StepBase):
    """A turn that is structurally valid but semantically invalid.

    The fake returns ``result`` unchanged; the real ``ProviderOutputValidator``
    must reject it (un-allowlisted tool, bad arguments, out-of-scope request,
    unowned evidence, a non-declarable stop, or an empty turn).
    """

    kind: Literal["malformed"] = "malformed"
    result: AgentTurnResult


class SchemaViolationStep(_StepBase):
    """A turn whose raw payload does not even parse as an ``AgentTurnResult``.

    The fake attempts ``AgentTurnResult.model_validate(raw_payload)`` and lets
    the Pydantic ``ValidationError`` propagate, exercising the strict schema
    surface of the provider contract.
    """

    kind: Literal["schema_violation"] = "schema_violation"
    raw_payload: dict[str, Any]


class ErrorStep(_StepBase):
    """A recoverable provider failure (rate limit, transport blip, ...)."""

    kind: Literal["error"] = "error"
    message: str = Field(min_length=1, max_length=2_000)
    retryable: bool = True


class CrashStep(_StepBase):
    """A non-retryable provider crash."""

    kind: Literal["crash"] = "crash"
    message: str = Field(min_length=1, max_length=2_000)


FakeScriptStep = Annotated[
    (
        RequestToolsStep
        | DelegateChildStep
        | StopStep
        | MalformedStep
        | SchemaViolationStep
        | ErrorStep
        | CrashStep
    ),
    Field(discriminator="kind"),
]


class ScriptNotFound(Exception):
    """Raised when a requested script step index does not exist."""


class ScriptExhausted(Exception):
    """Raised when a run's script has no steps left to replay."""


class FakeProviderRegistry:
    """Persistently addressable script steps, keyed by run id.

    Scripts survive across turns and across ``FakeProvider`` instances as long
    as they share the registry, so a test can arrange a run's steps up front
    and replay them turn after turn, peeking or indexing into them at will.
    The registry also records every ``ConversationRequest`` a run received, so
    tests can assert over the continuous message history the provider saw.

    A script item may be a ``FakeScriptStep`` *or* a concrete ``BaseException``
    instance (e.g. ``PromptTooLongError()``): the provider raises exception
    items on pop, which lets a test arrange a provider failure without wrapping
    it in an ``ErrorStep``.
    """

    def __init__(self) -> None:
        self._scripts: dict[str, list[object]] = {}
        self._requests: dict[str, list[ConversationRequest]] = {}

    def set_script(self, run_id: str, steps: Sequence[object]) -> None:
        """Replace the run's script with ``steps``, replayed in order."""
        self._scripts[run_id] = list(steps)

    def script(self, run_id: str, steps: Sequence[object] | None = None) -> tuple[object, ...]:
        """Set or read the run's script, oldest first.

        With ``steps``, replace the run's script and return the new tuple;
        without, return a copy of the run's pending steps.
        """
        if steps is not None:
            self._scripts[run_id] = list(steps)
        return tuple(self._scripts.get(run_id, ()))

    def append_step(self, run_id: str, step: object) -> None:
        """Append one step to the run's script, creating it if needed."""
        self._scripts.setdefault(run_id, []).append(step)

    def set_pending_script(self, steps: Sequence[object]) -> None:
        """Bind a script to the next run id requested by the provider."""
        self.set_script("__next__", steps)

    def has_script(self, run_id: str) -> bool:
        """Return True when the run has at least one pending step."""
        return bool(self._scripts.get(run_id))

    def remaining(self, run_id: str) -> int:
        """Return how many steps the run still has to replay."""
        return len(self._scripts.get(run_id, ()))

    def peek(self, run_id: str, index: int = 0) -> object:
        """Return step ``index`` of the run's script without consuming it."""
        steps = self._scripts.get(run_id, ())
        if index < 0 or index >= len(steps):
            raise ScriptNotFound(
                f"no script step {index} for run {run_id!r} ({len(steps)} pending)"
            )
        return steps[index]

    def pop(self, run_id: str) -> object:
        """Consume and return the run's next step, raising when exhausted."""
        steps = self._scripts.get(run_id)
        if not steps:
            raise ScriptExhausted(f"no script steps left for run {run_id!r}")
        return steps.pop(0)

    def record_request(self, run_id: str, request: ConversationRequest) -> None:
        """Append one provider request and bind a pending script to this run."""
        pending = self._scripts.pop("__next__", None)
        if pending is not None and run_id not in self._scripts:
            self._scripts[run_id] = pending
        self._requests.setdefault(run_id, []).append(request)

    def requests(self, run_id: str) -> tuple[ConversationRequest, ...]:
        """Return a copy of the requests recorded for the run, oldest first."""
        return tuple(self._requests.get(run_id, ()))

    def clear(self) -> None:
        """Drop every registered script and recorded request (test teardown)."""
        self._scripts.clear()
        self._requests.clear()


class FakeProvider(ModelProvider):
    """A scripted provider that replays registry steps for the requested run.

    Every ``ConversationRequest`` the provider receives is recorded on the
    registry so tests can assert over the continuous message history.
    """

    def __init__(self, registry: FakeProviderRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> FakeProviderRegistry:
        return self._registry

    async def generate_turn(self, request: ConversationRequest) -> AgentTurnResult:
        self._registry.record_request(request.checkpoint.agent_run_id, request)
        step = self._registry.pop(request.checkpoint.agent_run_id)
        if isinstance(step, BaseException):
            # A script item may be a concrete exception (e.g.
            # ``PromptTooLongError()``) that the provider raises on pop.
            raise step
        if isinstance(step, RequestToolsStep):
            return AgentTurnResult(
                tool_requests=step.tool_requests,
                hypotheses=step.hypotheses,
                conclusions=(step.conclusion,) if step.conclusion is not None else (),
                usage=step.usage,
            )
        if isinstance(step, DelegateChildStep):
            return AgentTurnResult(child_delegation=step.delegation, usage=step.usage)
        if isinstance(step, StopStep):
            conclusion = step.conclusion
            if conclusion is not None and "__latest__" in conclusion.evidence_ids:

                def collect_ids(value: object) -> tuple[str, ...]:
                    if isinstance(value, dict):
                        ids = value.get("evidence_ids", ())
                        return tuple(ids) + tuple(
                            item_id for item in value.values() for item_id in collect_ids(item)
                        )
                    if isinstance(value, (tuple, list)):
                        return tuple(item_id for item in value for item_id in collect_ids(item))
                    return ()

                evidence_ids = tuple(
                    dict.fromkeys(
                        item_id
                        for message in request.messages
                        for item_id in collect_ids(message.model_dump(mode="json"))
                    )
                )
                conclusion = conclusion.model_copy(update={"evidence_ids": evidence_ids})
            return AgentTurnResult(
                stop_signal=step.stop_signal,
                hypotheses=step.hypotheses,
                conclusions=(conclusion,) if conclusion is not None else (),
                usage=step.usage,
            )
        if isinstance(step, MalformedStep):
            return step.result
        if isinstance(step, SchemaViolationStep):
            return AgentTurnResult.model_validate(step.raw_payload)
        if isinstance(step, ErrorStep):
            raise ProviderError(step.message, retryable=step.retryable)
        if isinstance(step, CrashStep):
            raise ProviderCrash(step.message)
        raise AssertionError(f"unknown script step: {step!r}")

    # -- registry passthroughs used by the orchestrator test harness ----------

    def script(self, run_id: str, steps: Sequence[object] | None = None) -> tuple[object, ...]:
        """Set or read the run's script through the shared registry."""
        return self._registry.script(run_id, steps)

    def requests(self, run_id: str) -> tuple[ConversationRequest, ...]:
        """Return a copy of the requests recorded for the run, oldest first."""
        return self._registry.requests(run_id)

    def call_count(self, run_id: str) -> int:
        """Return how many provider calls the run has received."""
        return len(self._registry.requests(run_id))


__all__ = [
    "CrashStep",
    "DelegateChildStep",
    "ErrorStep",
    "FakeProvider",
    "FakeProviderRegistry",
    "FakeScriptStep",
    "MalformedStep",
    "RequestToolsStep",
    "SchemaViolationStep",
    "ScriptExhausted",
    "ScriptNotFound",
    "StopStep",
]
