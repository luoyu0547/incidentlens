"""Token-bounded active context and deterministic session memory.

IncidentLens does not persist the raw LLM transcript as the provider sees it:
the durable source of truth is the append-only ``agent_transcript_messages``
table, the compact boundaries, the versioned ``SessionMemory`` revisions, the
work plan (``agent_todos``) and the domain stores.  This module materializes
one bounded provider context per turn from that state.

The materialization pipeline is layered and every layer is deterministic:

1. ``tool_result_budget`` bounds each tool-result preview; the complete output
   stays in the EvidenceStore and is referenced by ``evidence_ids``.
2. ``snip_groups`` keeps the newest ``max_message_groups`` groups, never
   splitting a tool request from its matching result and never dropping a
   protected group (pending approval, failed/uncertain result, unmatched child
   notification).
3. ``micro_compact`` stubs old succeeded tool results whose full output was
   persisted, keeping the most recent results verbatim.
4. When the active context still exceeds ``max_input_tokens``, a deterministic
   ``SessionMemory`` revision is built from current-run durable state and a
   compact boundary advances the transcript coverage.
5. Only after memory exists may the oldest eligible recent groups be dropped to
   fit the budget; the context header, Todo plan, protected results and child
   notifications are never dropped.

Token estimation is deliberately conservative (``ConservativeTokenEstimator``)
and can only be *calibrated down* -- never optimistically up -- from real
provider usage.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ValidationError

from incidentlens_control_plane.investigation.compactor import (
    CompactionCircuitOpen,
    CompactionRejected,
    CompactionRequest,
    CompactionValidator,
    ContextCompactor,
)
from incidentlens_control_plane.investigation.state_machine import HypothesisStatus
from incidentlens_control_plane.investigation.store import (
    CompactBoundaryConflict,
    DelegatedTaskNotFound,
    InvestigationStore,
    MemoryConflict,
)
from incidentlens_control_plane.investigation.transcript import (
    MessageGroup,
    TranscriptService,
)
from incidentlens_control_plane.investigation.types import (
    AgentRun,
    CompactBoundary,
    CompactionState,
    Investigation,
    MessageRole,
    SessionMemory,
    TextBlock,
    TodoItem,
    TodoStatus,
    ToolCallStatus,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
)
from incidentlens_control_plane.logs.redaction import redact_message

if TYPE_CHECKING:
    from incidentlens_control_plane.investigation.provider import ToolSchema

# Statuses that mark a tool result as information the active context must never
# lose: a pending approval parks the run on a human decision, and a failed or
# uncertain result records an unconfirmable / rejected mutation.
_PROTECTED_RESULT_STATUSES: frozenset[ToolCallStatus] = frozenset(
    {
        ToolCallStatus.FAILED,
        ToolCallStatus.UNCERTAIN,
        ToolCallStatus.WAITING_APPROVAL,
    }
)


def _json_default(value: object) -> object:
    """Convert pydantic models / datetimes / paths for ``json.dumps``."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, PurePosixPath):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """The computed token budget for one materialized provider context.

    ``max_input_tokens`` is the ceiling the provider context may consume;
    ``input_tokens`` is the conservative estimate of the actual input the
    current context would consume (system prompt + attachments, serialized tool
    schemas and all active messages).
    """

    context_window: int
    max_output_tokens: int
    reserve_tokens: int
    system_tokens: int
    tool_tokens: int
    message_tokens: int

    @property
    def max_input_tokens(self) -> int:
        return self.context_window - self.max_output_tokens - self.reserve_tokens

    @property
    def input_tokens(self) -> int:
        return self.system_tokens + self.tool_tokens + self.message_tokens


@dataclass(frozen=True, slots=True)
class ContextBudgetPolicy:
    """Hard bounds for active-context materialization.

    The token window, output reservation and deterministic compaction limits
    mirror the runtime settings Task 7 wires into the manager.
    """

    context_window: int = 128_000
    max_output_tokens: int = 8_000
    reserve_tokens: int = 13_000
    tool_result_budget_chars: int = 200_000
    max_message_groups: int = 50
    keep_recent_tool_results: int = 3
    system_prompt: str = ""

    def __post_init__(self) -> None:
        if self.context_window < 8_000:
            raise ValueError("context_window must be >= 8000")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be >= 1")
        if self.reserve_tokens < 0:
            raise ValueError("reserve_tokens must be >= 0")
        if self.context_window <= self.max_output_tokens + self.reserve_tokens:
            raise ValueError("context_window must exceed max_output_tokens + reserve_tokens")
        if self.tool_result_budget_chars < 10_000:
            raise ValueError("tool_result_budget_chars must be >= 10000")
        if self.max_message_groups < 1:
            raise ValueError("max_message_groups must be >= 1")
        if self.keep_recent_tool_results < 1:
            raise ValueError("keep_recent_tool_results must be >= 1")


class TokenEstimator(Protocol):
    """Count the tokens a piece of provider-visible content would consume."""

    def count_text(self, text: str) -> int: ...

    def count_json(self, value: object) -> int: ...


class ConservativeTokenEstimator:
    """A deliberately conservative character-per-token estimator.

    The default ``chars_per_token`` (2.5) is a safe upper bound for most
    provider tokenizers.  ``calibrate`` can only *lower* the ratio (which
    raises the estimate), so live provider usage can never make the budget
    optimistic about a context it has already seen overflow.
    """

    def __init__(self, chars_per_token: float = 2.5) -> None:
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        self._chars_per_token = chars_per_token

    def count_text(self, text: str) -> int:
        return max(1, math.ceil(len(text) / self._chars_per_token))

    def count_json(self, value: object) -> int:
        return self.count_text(json.dumps(value, default=_json_default, sort_keys=True))

    def calibrate(self, *, actual_input_tokens: int, estimated_input_tokens: int) -> None:
        """Lower ``chars_per_token`` when a live provider exceeded the estimate.

        ``actual > estimated`` means the estimator was too optimistic (the real
        tokenizer is more expensive per character).  The ratio is lowered so a
        subsequent estimate counts more tokens; an estimate that already
        covered the actual usage is never nudged in the optimistic direction.
        """
        if estimated_input_tokens <= 0 or actual_input_tokens <= estimated_input_tokens:
            return
        implied = self._chars_per_token * estimated_input_tokens / actual_input_tokens
        if implied < self._chars_per_token:
            self._chars_per_token = implied


@dataclass(frozen=True, slots=True)
class ActiveContext:
    """The bounded, provider-consumable context for one turn."""

    messages: tuple[TranscriptMessage, ...]
    budget: ContextBudget
    memory: SessionMemory | None
    todos: tuple[TodoItem, ...]


# -- deterministic group transforms -------------------------------------------


def _is_protected_group(group: MessageGroup) -> bool:
    """Return True when the group must never be dropped or stubbed.

    A group is protected when it carries a tool result that is failed,
    uncertain or waiting approval, or when it is an unmatched child-report
    notification.  These carry state the active context must always surface.
    """
    for message in group.messages:
        for block in message.blocks:
            if isinstance(block, ToolResultBlock):
                if block.status in _PROTECTED_RESULT_STATUSES:
                    return True
            if isinstance(block, TextBlock):
                lowered = block.text.lower()
                if "child" in lowered and (
                    "report" in lowered or "completed" in lowered or "stopped" in lowered
                ):
                    return True
    return False


def tool_result_budget(
    groups: tuple[MessageGroup, ...], *, max_chars: int
) -> tuple[MessageGroup, ...]:
    """Bound every tool-result preview to ``max_chars``.

    A result whose content exceeds the budget is truncated to a bounded preview
    and marked ``persisted_output=True``; the complete content stays in the
    EvidenceStore behind the block's ``evidence_ids``.  Groups and tool
    pairings are otherwise untouched.
    """
    rebuilt_groups: list[MessageGroup] = []
    for group in groups:
        rebuilt_messages: list[TranscriptMessage] = []
        for message in group.messages:
            blocks: list[object] = []
            for block in message.blocks:
                if isinstance(block, ToolResultBlock) and len(block.content) > max_chars:
                    preview = (
                        block.content[:max_chars]
                        + f"\n...[output truncated to {max_chars} chars; "
                        + "full content persisted in EvidenceStore]"
                    )
                    blocks.append(
                        block.model_copy(
                            update={"content": preview, "persisted_output": True}
                        )
                    )
                else:
                    blocks.append(block)
            rebuilt_messages.append(message.model_copy(update={"blocks": tuple(blocks)}))
        rebuilt_groups.append(MessageGroup(tuple(rebuilt_messages)))
    return tuple(rebuilt_groups)


def snip_groups(
    groups: tuple[MessageGroup, ...], *, max_groups: int
) -> tuple[MessageGroup, ...]:
    """Keep the newest ``max_groups`` groups plus any protected dropped ones.

    The cut is group-atomic: a tool-use message and its matching result are
    kept or dropped together.  A protected group (failed/uncertain/approval,
    child notification) is re-appended even when it falls inside the dropped
    prefix, so the active context never loses it.
    """
    if len(groups) <= max_groups:
        return groups
    dropped = groups[:-max_groups]
    protected = tuple(group for group in dropped if _is_protected_group(group))
    return protected + groups[-max_groups:]


def _stub_succeeded_result(group: MessageGroup) -> MessageGroup:
    """Replace a succeeded tool result's content with a bounded evidence stub."""
    rebuilt_messages: list[TranscriptMessage] = []
    for message in group.messages:
        blocks: list[object] = []
        for block in message.blocks:
            if isinstance(block, ToolResultBlock):
                stub = (
                    f"[output persisted in EvidenceStore ({len(block.content)} chars); "
                    f"reload via {list(block.evidence_ids)} on demand]"
                )
                blocks.append(
                    block.model_copy(
                        update={"content": stub, "persisted_output": True}
                    )
                )
            else:
                blocks.append(block)
        rebuilt_messages.append(message.model_copy(update={"blocks": tuple(blocks)}))
    return MessageGroup(tuple(rebuilt_messages))


def micro_compact(
    groups: tuple[MessageGroup, ...], *, keep_recent: int
) -> tuple[MessageGroup, ...]:
    """Stub old succeeded tool results whose output was persisted.

    The most recent ``keep_recent`` succeeded tool-result groups stay verbatim;
    older succeeded results (whose complete content lives in the EvidenceStore)
    are reduced to a bounded stub.  Protected groups are never stubbed and tool
    pairs are never split.
    """
    succeeded_total = sum(1 for group in groups if _is_succeeded_tool_pair(group))
    stub_until = succeeded_total - keep_recent
    succeeded_seen = 0
    rebuilt: list[MessageGroup] = []
    for group in groups:
        if _is_protected_group(group):
            rebuilt.append(group)
            continue
        if _is_succeeded_tool_pair(group):
            succeeded_seen += 1
            if succeeded_seen <= stub_until:
                rebuilt.append(_stub_succeeded_result(group))
                continue
        rebuilt.append(group)
    return tuple(rebuilt)


def _is_succeeded_tool_pair(group: MessageGroup) -> bool:
    """Return True when the group is an assistant tool-use/result pair that succeeded."""
    has_use = False
    has_result = False
    for message in group.messages:
        for block in message.blocks:
            if isinstance(block, ToolResultBlock) and block.status is ToolCallStatus.SUCCEEDED:
                has_result = True
    if not has_result:
        return False
    for message in group.messages:
        if any(isinstance(block, ToolUseBlock) for block in message.blocks):
            has_use = True
    return has_use


def flatten(groups: tuple[MessageGroup, ...]) -> tuple[TranscriptMessage, ...]:
    """Flatten groups back into an ordered transcript message tuple."""
    return tuple(message for group in groups for message in group.messages)


# -- context header -----------------------------------------------------------


def restore_context_header(
    run: AgentRun,
    investigation: Investigation,
    memory: SessionMemory | None,
    todos: tuple[TodoItem, ...],
    *,
    task_prompt: str | None = None,
) -> tuple[TranscriptMessage, ...]:
    """Build the fixed leading user message for a provider context.

    The header carries the investigation/run context, the optional delegated
    child task, the latest session memory and the current work plan.  It is
    synthesized per build (never persisted) and is therefore immune to snip and
    micro-compaction.
    """
    parts: list[str] = [
        f"Investigation {investigation.investigation_id} | incident "
        f"{investigation.incident_id} | service {investigation.service}",
        f"Symptom: {investigation.symptom}",
        f"Run {run.agent_run_id} ({run.kind.value}) status={run.status.value} "
        f"rounds={run.usage.rounds} scope={run.scope.scope.value}",
    ]
    if task_prompt:
        parts.append(f"Delegated task: {task_prompt}")
    if memory is not None:
        parts.append(
            f"Session memory (revision {memory.revision}, through transcript "
            f"sequence {memory.through_transcript_sequence}):"
        )
        parts.append(memory.model_dump_json())
    if todos:
        parts.append("Work plan:")
        for item in todos:
            parts.append(f"- [{item.status.value}] {item.content}")
    text = "\n".join(parts)
    return (
        TranscriptMessage(
            agent_run_id=run.agent_run_id,
            sequence=1,
            role=MessageRole.USER,
            blocks=(TextBlock(text=text),),
            created_at=run.updated_at,
        ),
    )


class AgentContextManager:
    """Build bounded, token-budgeted provider contexts from durable state.

    ``build`` runs the deterministic compaction pipeline in the fixed order
    (tool-result budget -> snip -> micro-compact -> header + flatten), then, if
    the materialized context still exceeds the input budget, appends a
    deterministic ``SessionMemory`` revision and advances a ``CompactBoundary``
    before re-materializing from the new boundary.
    """

    def __init__(
        self,
        store: InvestigationStore,
        *,
        policy: ContextBudgetPolicy | None = None,
        estimator: TokenEstimator | None = None,
        now: Callable[[], datetime] | None = None,
        compactor: ContextCompactor | None = None,
        validator: CompactionValidator | None = None,
    ) -> None:
        self._store = store
        self._policy = policy or ContextBudgetPolicy()
        self._estimator = estimator or ConservativeTokenEstimator()
        self._now = now or (lambda: datetime.now(UTC))
        self._compactor = compactor
        self._validator = validator or CompactionValidator()
        self._transcript = TranscriptService(store)

    # -- public ---------------------------------------------------------------

    def build(
        self,
        run: AgentRun,
        investigation: Investigation,
        tool_schemas: tuple[ToolSchema, ...],
    ) -> ActiveContext:
        """Materialize one bounded active context for a provider turn."""
        boundary = self._store.get_latest_compact_boundary(run.agent_run_id)
        memory = self._store.get_latest_session_memory(run.agent_run_id)
        todos = self._store.list_todos(run.agent_run_id)

        active = self._materialize(run, investigation, tool_schemas, boundary, memory, todos)
        if active.budget.input_tokens <= active.budget.max_input_tokens:
            return active

        # Context pressure: build a deterministic memory revision, advance the
        # coverage boundary and re-materialize from it.
        memory = self._compact(run, investigation, boundary, memory, todos, tool_schemas)
        boundary = self._store.get_latest_compact_boundary(run.agent_run_id)
        active = self._materialize(run, investigation, tool_schemas, boundary, memory, todos)

        if (
            active.budget.input_tokens > active.budget.max_input_tokens
            and memory is not None
        ):
            # Only the oldest eligible recent groups may be dropped once a
            # Session Memory exists to summarize them.  When no memory could be
            # built (nothing eligible to cover), dropping would lose history
            # the active context would never see again, so the over-budget
            # context is returned unchanged instead.  Protected groups are
            # never dropped.
            active = self._trim_to_budget(
                run, investigation, tool_schemas, boundary, memory, todos
            )
        return active

    # -- semantic / reactive compaction ---------------------------------------

    async def semantic_compact(self, run: AgentRun) -> SessionMemory:
        """Ask the injected ``ContextCompactor`` to summarize the recent transcript.

        The manager builds a tool-free ``CompactionRequest`` over the groups
        written since the latest compact boundary, validates the compactor's
        memory revision (evidence ownership, monotonic boundary, redaction and
        length) and commits it with its boundary and a reset breaker state in
        one transaction.  A provider failure or an invalid revision increments
        the durable breaker; the fourth consecutive failure raises
        ``CompactionCircuitOpen``.
        """
        boundary = self._store.get_latest_compact_boundary(run.agent_run_id)
        groups = self._transcript.group_messages(
            run.agent_run_id, after=boundary.through_sequence if boundary else 0
        )
        return await self._semantic_compact_groups(run, groups)

    async def reactive_compact(
        self, run: AgentRun, *, keep_recent_groups: int = 5
    ) -> ActiveContext:
        """Respond to a prompt-too-long turn by compacting the oldest groups.

        The newest ``keep_recent_groups`` complete groups stay whole in the
        replayed tail; everything older is handed to the semantic compactor and
        summarized into a new memory revision, after which the context is
        re-materialized from the new boundary with the tail preserved.  Only one
        reactive compaction is allowed per round: a second attempt in the same
        round is refused using ``CompactionState.reactive_round``.
        """
        state = self._store.get_compaction_state(run.agent_run_id)
        if state is not None and state.reactive_round == run.usage.rounds:
            raise CompactionRejected(
                f"reactive compaction already performed for run {run.agent_run_id} "
                f"in round {run.usage.rounds}"
            )
        if keep_recent_groups < 1:
            raise ValueError("keep_recent_groups must be >= 1")
        investigation = self._store.get_investigation(run.investigation_id)
        groups = self._transcript.group_messages(run.agent_run_id)
        head, tail = groups[:-keep_recent_groups], groups[-keep_recent_groups:]
        memory = await self._semantic_compact_groups(
            run, head, reactive_round=run.usage.rounds
        )
        return self._materialize_after(
            run, investigation, memory.through_transcript_sequence, tail
        )

    async def _semantic_compact_groups(
        self,
        run: AgentRun,
        groups: tuple[MessageGroup, ...],
        *,
        reactive_round: int | None = None,
    ) -> SessionMemory:
        """Compact ``groups`` through the injected compactor and commit atomically."""
        state = self._store.get_compaction_state(run.agent_run_id)
        if state is not None and state.consecutive_failures >= 3:
            raise CompactionCircuitOpen(
                f"semantic compaction tripped the breaker for run {run.agent_run_id}: "
                f"{state.consecutive_failures} consecutive failures"
            )
        if self._compactor is None:
            raise CompactionRejected(
                f"no semantic compactor configured for run {run.agent_run_id}"
            )
        boundary = self._store.get_latest_compact_boundary(run.agent_run_id)
        prior_memory = self._store.get_latest_session_memory(run.agent_run_id)
        if not groups and boundary is not None and prior_memory is not None:
            # Nothing new to summarize: the existing memory and boundary already
            # cover the whole transcript, so a redundant commit would collide on
            # the boundary.  Return the prior revision unchanged.
            return prior_memory
        through_sequence = (
            max(message.sequence for group in groups for message in group.messages)
            if groups
            else (boundary.through_sequence if boundary else 0)
        )
        messages = flatten(
            tool_result_budget(groups, max_chars=self._policy.tool_result_budget_chars)
        )
        request = CompactionRequest(
            agent_run_id=run.agent_run_id,
            through_sequence=through_sequence,
            prior_memory=prior_memory,
            messages=messages,
            allowed_evidence_ids=tuple(
                dict.fromkeys(reference.evidence_id for reference in run.evidence)
            ),
        )
        try:
            memory = await self._compactor.compact(request)
        except CompactionCircuitOpen:
            raise
        except CompactionRejected:
            self._record_failure(run, state)
            raise
        except Exception as exc:
            self._record_failure(run, state)
            raise CompactionRejected(
                f"semantic compactor failed for run {run.agent_run_id}: {exc}"
            ) from exc
        try:
            memory = self._validator.validate(request, memory)
        except CompactionRejected:
            self._record_failure(run, state)
            raise
        except ValidationError as exc:
            self._record_failure(run, state)
            raise CompactionRejected(
                f"semantic compactor returned an invalid memory for run "
                f"{run.agent_run_id}: {exc}"
            ) from exc
        if memory.investigation_id != run.investigation_id:
            self._record_failure(run, state)
            raise CompactionRejected(
                f"semantic compactor memory names investigation "
                f"{memory.investigation_id!r} but the run belongs to "
                f"{run.investigation_id!r}"
            )
        compact_boundary = CompactBoundary(
            agent_run_id=run.agent_run_id,
            through_sequence=memory.through_transcript_sequence,
            memory_revision=memory.revision,
            summary=(
                f"semantic compact through transcript sequence "
                f"{memory.through_transcript_sequence}"
            ),
            created_at=self._now(),
        )
        next_state = CompactionState(
            agent_run_id=run.agent_run_id,
            consecutive_failures=0,
            reactive_round=(
                reactive_round
                if reactive_round is not None
                else (state.reactive_round if state else None)
            ),
            latest_boundary_sequence=memory.through_transcript_sequence,
            updated_at=self._now(),
        )
        self._store.commit_compaction(memory, compact_boundary, next_state)
        return memory

    def _materialize_after(
        self,
        run: AgentRun,
        investigation: Investigation,
        through_sequence: int,
        tail: tuple[MessageGroup, ...],
    ) -> ActiveContext:
        """Rebuild a bounded active context after a reactive compaction.

        Groups the new memory already summarizes (``sequence <=
        through_sequence``) are never replayed; the preserved ``tail`` is
        otherwise kept whole, budgeted and micro-compacted like any active
        context, then replayed behind the header carrying the fresh memory.
        Tool schemas are not available on this path, so the budget estimate
        counts no tool tokens.
        """
        memory = self._store.get_latest_session_memory(run.agent_run_id)
        todos = self._store.list_todos(run.agent_run_id)
        tail = tuple(
            group
            for group in tail
            if any(message.sequence > through_sequence for message in group.messages)
        )
        groups = tool_result_budget(tail, max_chars=self._policy.tool_result_budget_chars)
        groups = micro_compact(groups, keep_recent=self._policy.keep_recent_tool_results)
        messages = restore_context_header(
            run, investigation, memory, todos, task_prompt=self._task_prompt(run)
        ) + flatten(groups)
        budget = self._estimate_budget(run, investigation, messages, ())
        return ActiveContext(
            messages=messages, budget=budget, memory=memory, todos=todos
        )

    def _record_failure(self, run: AgentRun, state: CompactionState | None) -> None:
        """Persist one more consecutive compaction failure (separate transaction)."""
        prior = state or CompactionState(
            agent_run_id=run.agent_run_id, updated_at=self._now()
        )
        incremented = prior.model_copy(
            update={
                "consecutive_failures": prior.consecutive_failures + 1,
                "updated_at": self._now(),
            }
        )
        self._store.put_compaction_state(incremented)

    # -- materialization ------------------------------------------------------

    def _materialize(
        self,
        run: AgentRun,
        investigation: Investigation,
        tool_schemas: tuple[ToolSchema, ...],
        boundary: CompactBoundary | None,
        memory: SessionMemory | None,
        todos: tuple[TodoItem, ...],
    ) -> ActiveContext:
        groups = self._transcript.group_messages(
            run.agent_run_id, after=boundary.through_sequence if boundary else 0
        )
        groups = tool_result_budget(groups, max_chars=self._policy.tool_result_budget_chars)
        groups = snip_groups(groups, max_groups=self._policy.max_message_groups)
        groups = micro_compact(groups, keep_recent=self._policy.keep_recent_tool_results)
        messages = restore_context_header(
            run, investigation, memory, todos, task_prompt=self._task_prompt(run)
        ) + flatten(groups)
        budget = self._estimate_budget(run, investigation, messages, tool_schemas)
        return ActiveContext(
            messages=messages, budget=budget, memory=memory, todos=todos
        )

    def _trim_to_budget(
        self,
        run: AgentRun,
        investigation: Investigation,
        tool_schemas: tuple[ToolSchema, ...],
        boundary: CompactBoundary | None,
        memory: SessionMemory | None,
        todos: tuple[TodoItem, ...],
    ) -> ActiveContext:
        """Drop the oldest eligible recent groups until the context fits.

        Precondition: ``memory`` is not ``None`` — the dropped groups must be
        summarized by a Session Memory revision, otherwise their history would
        be lost from every future context.  The caller only invokes this after
        a memory revision exists; protected groups are never dropped.
        """
        groups = self._transcript.group_messages(
            run.agent_run_id, after=boundary.through_sequence if boundary else 0
        )
        groups = tool_result_budget(groups, max_chars=self._policy.tool_result_budget_chars)
        groups = micro_compact(groups, keep_recent=self._policy.keep_recent_tool_results)
        while True:
            messages = restore_context_header(
                run, investigation, memory, todos, task_prompt=self._task_prompt(run)
            ) + flatten(groups)
            budget = self._estimate_budget(run, investigation, messages, tool_schemas)
            if budget.input_tokens <= budget.max_input_tokens or len(groups) <= 1:
                break
            dropped = False
            for index, group in enumerate(groups):
                if not _is_protected_group(group):
                    groups = groups[:index] + groups[index + 1 :]
                    dropped = True
                    break
            if not dropped:
                break
        return ActiveContext(
            messages=messages, budget=budget, memory=memory, todos=todos
        )

    # -- deterministic compaction ---------------------------------------------

    def _compact(
        self,
        run: AgentRun,
        investigation: Investigation,
        boundary: CompactBoundary | None,
        latest_memory: SessionMemory | None,
        todos: tuple[TodoItem, ...],
        tool_schemas: tuple[ToolSchema, ...],
    ) -> SessionMemory:
        groups = self._transcript.group_messages(
            run.agent_run_id, after=boundary.through_sequence if boundary else 0
        )
        groups = tool_result_budget(groups, max_chars=self._policy.tool_result_budget_chars)
        fields = self._memory_fields(run, investigation, todos)
        coverage = self._deterministic_coverage(
            groups, run, investigation, latest_memory, todos, fields, tool_schemas
        )
        if coverage <= (boundary.through_sequence if boundary else 0):
            # Nothing eligible to cover: the context cannot shrink by compacting.
            return latest_memory
        memory = self._build_memory(run, investigation, latest_memory, coverage, fields)
        try:
            stored = self._store.append_session_memory(memory)
        except MemoryConflict:
            stored = self._store.get_latest_session_memory(run.agent_run_id)
            if stored is None:
                raise
        compact_boundary = CompactBoundary(
            agent_run_id=run.agent_run_id,
            through_sequence=coverage,
            memory_revision=stored.revision,
            summary=f"deterministic compact through transcript sequence {coverage}",
            created_at=self._now(),
        )
        try:
            self._store.append_compact_boundary(compact_boundary)
        except CompactBoundaryConflict:
            # A concurrent builder already advanced the boundary; reuse it.
            pass
        return stored

    def _deterministic_coverage(
        self,
        groups: tuple[MessageGroup, ...],
        run: AgentRun,
        investigation: Investigation,
        latest_memory: SessionMemory | None,
        todos: tuple[TodoItem, ...],
        fields: dict[str, object],
        tool_schemas: tuple[ToolSchema, ...],
    ) -> int:
        """Return the transcript sequence the next memory revision covers.

        The boundary is a contiguous sequence threshold: every group whose
        messages are all ``<= boundary`` is summarized by the new memory, and
        the rest form the replayed tail.  The tail is sized against
        ``max_input_tokens`` using the sizes the groups will actually have after
        micro-compaction (old succeeded tool results are stubbed, recent ones
        stay verbatim), and the largest boundary whose tail fits is chosen.  A
        protected group is never covered: the boundary is capped below the first
        protected group so a failed/uncertain/approval result or a child
        notification is always replayed.
        """
        budget = self._policy.context_window - (
            self._policy.max_output_tokens + self._policy.reserve_tokens
        )
        system_tokens = self._estimator.count_text(self._policy.system_prompt)
        system_tokens += self._estimator.count_json(self._checkpoint_dump(run))
        system_tokens += self._estimator.count_json(self._snapshot_dump(investigation))
        tool_tokens = sum(
            self._estimator.count_json(schema.model_dump()) for schema in tool_schemas
        )
        header_tokens = sum(
            self._estimator.count_json(message.model_dump())
            for message in restore_context_header(
                run, investigation, latest_memory, todos, task_prompt=self._task_prompt(run)
            )
        )
        # Conservative allowance for the new memory section that will appear in
        # the rebuilt header (the memory fields plus a few label lines).
        memory_allowance = self._estimator.count_json(fields) + 80
        tail_budget = budget - system_tokens - tool_tokens - header_tokens - memory_allowance

        # The boundary may never reach a protected group's first sequence.
        coverage_cap: int | None = None
        for group in groups:
            if _is_protected_group(group):
                first_sequence = min(message.sequence for message in group.messages)
                cap = first_sequence - 1
                coverage_cap = cap if coverage_cap is None else min(coverage_cap, cap)

        candidates = sorted(
            {max(message.sequence for message in group.messages) for group in groups}
        )
        best = 0
        for candidate in candidates:
            if coverage_cap is not None and candidate > coverage_cap:
                continue
            tail = tuple(
                group
                for group in groups
                if max(message.sequence for message in group.messages) > candidate
            )
            tail_tokens = sum(
                self._estimator.count_json(message.model_dump())
                for message in flatten(
                    micro_compact(
                        tail, keep_recent=self._policy.keep_recent_tool_results
                    )
                )
            )
            if tail_tokens <= tail_budget:
                # The smallest boundary whose tail fits keeps the most recent
                # context in the replayed tail; the trim step handles any
                # residual overflow after the memory header is added.
                best = candidate
                break
        return best

    def _build_memory(
        self,
        run: AgentRun,
        investigation: Investigation,
        latest_memory: SessionMemory | None,
        coverage: int,
        fields: dict[str, object],
    ) -> SessionMemory:
        revision = (latest_memory.revision + 1) if latest_memory else 1
        return SessionMemory(
            memory_id=f"mem-{run.agent_run_id}-{revision}",
            agent_run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            revision=revision,
            through_round=run.usage.rounds,
            through_transcript_sequence=coverage,
            created_at=self._now(),
            **fields,
        )

    def _memory_fields(
        self,
        run: AgentRun,
        investigation: Investigation,
        todos: tuple[TodoItem, ...],
    ) -> dict[str, object]:
        """Build the deterministic memory fields from current-run durable state."""
        conclusions = self._store.list_conclusions(agent_run_id=run.agent_run_id)
        tool_calls = self._store.list_tool_calls(agent_run_id=run.agent_run_id)

        confirmed_facts: list[str] = []
        for hypothesis in run.hypotheses:
            if hypothesis.status is HypothesisStatus.CONFIRMED:
                confirmed_facts.extend(hypothesis.facts)
        for conclusion in conclusions:
            confirmed_facts.extend(conclusion.facts)

        active_hypotheses = [
            hypothesis.summary
            for hypothesis in run.hypotheses
            if hypothesis.status
            in {HypothesisStatus.PROPOSED, HypothesisStatus.ACTIVE}
        ]
        open_questions = [
            question
            for hypothesis in run.hypotheses
            for question in hypothesis.unknowns
        ]
        for conclusion in conclusions:
            open_questions.extend(conclusion.unknowns)

        completed_actions = [
            f"{call.tool_name}: {call.status.value}"
            for call in tool_calls
            if call.finished_at is not None
        ]
        # No user-constraint store exists yet; the field stays empty (a later
        # task may reconcile it with a durable constraints source).
        user_constraints: list[str] = []
        next_actions = [
            item.content
            for item in todos
            if item.status in {TodoStatus.PENDING, TodoStatus.IN_PROGRESS}
        ]
        todo_labels = [f"[{item.status.value}] {item.content}" for item in todos]
        evidence_ids = tuple(dict.fromkeys(ref.evidence_id for ref in run.evidence))[-64:]

        return {
            "objective": self._clean(self._task_prompt(run) or investigation.symptom, width=4_000),
            "confirmed_facts": self._bounded_unique(confirmed_facts, limit=24, width=400),
            "active_hypotheses": self._bounded_unique(active_hypotheses, limit=16, width=400),
            "open_questions": self._bounded_unique(open_questions, limit=16, width=400),
            "completed_actions": self._bounded_unique(completed_actions, limit=24, width=240),
            "child_findings": (),
            "evidence_ids": evidence_ids,
            "user_constraints": self._bounded_unique(user_constraints, limit=16, width=240),
            "todos": tuple(todo_labels)[-64:],
            "next_actions": self._bounded_unique(next_actions, limit=16, width=240),
        }

    # -- budget estimation ----------------------------------------------------

    def _estimate_budget(
        self,
        run: AgentRun,
        investigation: Investigation,
        messages: tuple[TranscriptMessage, ...],
        tool_schemas: tuple[ToolSchema, ...],
    ) -> ContextBudget:
        system_tokens = self._estimator.count_text(self._policy.system_prompt)
        system_tokens += self._estimator.count_json(self._checkpoint_dump(run))
        system_tokens += self._estimator.count_json(self._snapshot_dump(investigation))
        tool_tokens = sum(
            self._estimator.count_json(schema.model_dump()) for schema in tool_schemas
        )
        message_tokens = sum(
            self._estimator.count_json(message.model_dump()) for message in messages
        )
        return ContextBudget(
            context_window=self._policy.context_window,
            max_output_tokens=self._policy.max_output_tokens,
            reserve_tokens=self._policy.reserve_tokens,
            system_tokens=system_tokens,
            tool_tokens=tool_tokens,
            message_tokens=message_tokens,
        )

    @staticmethod
    def _checkpoint_dump(run: AgentRun) -> dict[str, object]:
        """A bounded JSON view of the run checkpoint the provider attaches."""
        return {
            "agent_run_id": run.agent_run_id,
            "kind": run.kind.value,
            "status": run.status.value,
            "round_number": run.usage.rounds,
            "parent_run_id": run.parent_run_id,
            "scope": run.scope.model_dump(mode="json"),
            "budget": run.budget.model_dump(mode="json"),
            "usage": run.usage.model_dump(mode="json"),
        }

    @staticmethod
    def _snapshot_dump(investigation: Investigation) -> dict[str, object]:
        """A bounded JSON view of the investigation snapshot the provider attaches."""
        return {
            "investigation_id": investigation.investigation_id,
            "incident_id": investigation.incident_id,
            "service": investigation.service,
            "symptom": investigation.symptom,
            "status": investigation.status.value,
            "budget": investigation.budget.model_dump(mode="json"),
            "usage": investigation.usage.model_dump(mode="json"),
        }

    # -- helpers --------------------------------------------------------------

    def _task_prompt(self, run: AgentRun) -> str | None:
        if run.parent_run_id is None:
            return None
        try:
            return self._store.get_delegated_task(run.agent_run_id).task_prompt
        except DelegatedTaskNotFound:
            return None

    @staticmethod
    def _bounded_unique(
        values: list[str], *, limit: int, width: int
    ) -> tuple[str, ...]:
        unique: dict[str, None] = {}
        for value in values:
            cleaned = AgentContextManager._clean(value, width=width)
            if cleaned:
                unique.setdefault(cleaned, None)
        return tuple(unique)[-limit:]

    @staticmethod
    def _clean(value: str, *, width: int) -> str:
        normalized = " ".join(value.split())
        return redact_message(normalized, max_length=width).message_redacted


__all__ = [
    "ActiveContext",
    "AgentContextManager",
    "ConservativeTokenEstimator",
    "ContextBudget",
    "ContextBudgetPolicy",
    "TokenEstimator",
    "flatten",
    "micro_compact",
    "restore_context_header",
    "snip_groups",
    "tool_result_budget",
]
