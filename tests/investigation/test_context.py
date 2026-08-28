"""Layered context materialization and token-budget tests.

The three tests from the task brief are the load-bearing contracts:

- a large tool result is persisted in EvidenceStore before the active preview
  is materialized;
- snip never splits a tool request from its matching result;
- the token budget counts system prompt + tool schemas + messages and reserves
  maximum output + a safety reserve.

The remaining tests exercise the deterministic pipeline: tool-result budgeting,
protected-group snipping, micro-compaction, the deterministic Session Memory
build under context pressure, and the conservative estimator's calibration.
"""

from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.evidence.types import EvidenceKind, EvidenceRef
from incidentlens_control_plane.investigation.compactor import (
    CompactionRejected,
    CompactionRequest,
)
from incidentlens_control_plane.investigation.context import (
    AgentContextManager,
    ConservativeTokenEstimator,
    ContextBudgetPolicy,
    flatten,
    micro_compact,
    tool_result_budget,
)
from incidentlens_control_plane.investigation.provider import ToolSchema
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.transcript import MessageGroup
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    ChildReport,
    ChildReportStatus,
    CompactBoundary,
    EvidenceReference,
    Investigation,
    InvestigationBudget,
    MessageRole,
    SessionMemory,
    StopReason,
    TextBlock,
    TodoItem,
    TodoStatus,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope

NOW = datetime(2026, 8, 17, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path) -> InvestigationStore:
    """An InvestigationStore sharing one SQLite file with the EvidenceStore."""
    db = tmp_path / "context.db"
    factory = lambda: sqlite3.connect(db)  # noqa: E731
    store = InvestigationStore(factory)
    store.migrate()
    EvidenceStore(factory).migrate()
    return store


def manager(
    store: InvestigationStore,
    *,
    context_window: int | None = None,
    max_output_tokens: int | None = None,
    reserve_tokens: int | None = None,
    max_input_tokens: int | None = None,
    tool_result_budget_chars: int | None = None,
    micro_compact_after_seconds: int | None = None,
    system_prompt: str | None = None,
) -> AgentContextManager:
    """Build a manager with explicit budget/compaction overrides."""
    policy = ContextBudgetPolicy(
        context_window=context_window if context_window is not None else 128_000,
        max_output_tokens=max_output_tokens if max_output_tokens is not None else 8_000,
        reserve_tokens=reserve_tokens if reserve_tokens is not None else 13_000,
        tool_result_budget_chars=(
            tool_result_budget_chars
            if tool_result_budget_chars is not None
            else 200_000
        ),
        micro_compact_after_seconds=(
            micro_compact_after_seconds
            if micro_compact_after_seconds is not None
            else 3_600
        ),
        system_prompt=system_prompt or "",
    )
    if max_input_tokens is not None:
        policy = dataclasses.replace(
            policy,
            context_window=(
                max_input_tokens + policy.max_output_tokens + policy.reserve_tokens
            ),
        )
    return AgentContextManager(store, policy=policy, now=lambda: NOW)


def investigation() -> Investigation:
    return Investigation(
        investigation_id="inv-1",
        incident_id="inc-1",
        project_id="checkout",
        target_id="prod-a",
        service="orders",
        symptom="checkout requests return 502",
        status=InvestigationStatus.RUNNING,
        budget=InvestigationBudget(),
        usage=UsageCounters(),
        created_at=NOW,
        updated_at=NOW,
    )


def run() -> AgentRun:
    return AgentRun(
        agent_run_id="run-1",
        investigation_id="inv-1",
        kind=AgentRunKind.PARENT,
        scope=AgentScope(project_id="checkout", target_id="prod-a", scope=LogScope.HOST),
        status=AgentRunStatus.RUNNING,
        budget=AgentBudget(),
        usage=UsageCounters(),
        created_at=NOW,
        updated_at=NOW,
    )


def schemas() -> tuple[ToolSchema, ...]:
    return (
        ToolSchema(
            tool_name="logs.query",
            description="query logs",
            parameters_json_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        ),
    )


def large_schemas() -> tuple[ToolSchema, ...]:
    return tuple(
        ToolSchema(
            tool_name=f"tool_{index}",
            description=(
                "Read and query structured logs from the registered host or container. "
                "Returns bounded evidence references. " * 3
            ).strip(),
            parameters_json_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 200}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )
        for index in range(8)
    )


def persist_tool_output(store: InvestigationStore, *, content: str) -> EvidenceReference:
    """Persist a large redacted tool output and return its bounded reference."""
    evidence_store = EvidenceStore(store.connection_factory)
    ref = evidence_store.create(
        EvidenceRef(
            evidence_ref_id="ev-tool-1",
            incident_id="inc-1",
            evidence_kind=EvidenceKind.COMMAND_OUTPUT,
            agent_run_id="run-1",
            project_id="checkout",
            target_id="prod-a",
            service_name="orders",
            source_ref="op-tool-1",
            content_redacted=content,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            redaction_summary={},
            created_at=NOW,
            created_by="agent",
        )
    )
    return EvidenceReference(
        evidence_id=ref.evidence_ref_id,
        operation_id="op-tool-1",
        summary=content[:2_000],
    )


def append_budgeted_tool_result(
    store: InvestigationStore, *, evidence: EvidenceReference, preview_chars: int
) -> None:
    """Append user + assistant tool-use + persisted tool-result messages."""
    store.append_transcript_message(
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=1,
            role=MessageRole.USER,
            blocks=(TextBlock(text="inspect the tool output"),),
            created_at=NOW,
        )
    )
    store.append_transcript_message(
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=2,
            role=MessageRole.ASSISTANT,
            blocks=(
                ToolUseBlock(
                    tool_call_id="call-1",
                    tool_name="logs.query",
                    arguments={"query": "error"},
                ),
            ),
            created_at=NOW,
        )
    )
    store.append_transcript_message(
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=3,
            role=MessageRole.USER,
            blocks=(
                ToolResultBlock(
                    tool_call_id="call-1",
                    status=ToolCallStatus.SUCCEEDED,
                    content=evidence.summary[:preview_chars],
                    evidence_ids=(evidence.evidence_id,),
                    persisted_output=True,
                ),
            ),
            created_at=NOW,
        )
    )


def find_tool_result(
    messages: tuple[TranscriptMessage, ...], tool_call_id: str
) -> ToolResultBlock:
    for message in messages:
        for block in message.blocks:
            if isinstance(block, ToolResultBlock) and block.tool_call_id == tool_call_id:
                return block
    raise AssertionError(f"no ToolResultBlock for {tool_call_id!r} in active messages")


def assert_tool_pairs_are_complete(messages: tuple[TranscriptMessage, ...]) -> None:
    use_ids = {
        block.tool_call_id
        for message in messages
        for block in message.blocks
        if isinstance(block, ToolUseBlock)
    }
    result_ids = {
        block.tool_call_id
        for message in messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    }
    assert use_ids == result_ids, (
        f"tool pairs split: uses={sorted(use_ids)} results={sorted(result_ids)}"
    )


def seed_many_message_groups(
    store: InvestigationStore,
    *,
    count: int = 60,
    content_width: int = 300,
    fail_first: bool = False,
) -> None:
    """Append ``count`` assistant tool-use/tool-result message pairs."""
    for index in range(count):
        sequence = index * 2 + 1
        failed = fail_first and index == 0
        status = ToolCallStatus.FAILED if failed else ToolCallStatus.SUCCEEDED
        content = f"{'error' if failed else 'result'} {index} " + "x" * content_width
        store.append_transcript_message(
            TranscriptMessage(
                agent_run_id="run-1",
                sequence=sequence,
                role=MessageRole.ASSISTANT,
                blocks=(
                    ToolUseBlock(
                        tool_call_id=f"call-{index}",
                        tool_name="logs.query",
                        arguments={"query": f"error {index}"},
                    ),
                ),
                created_at=NOW,
            )
        )
        store.append_transcript_message(
            TranscriptMessage(
                agent_run_id="run-1",
                sequence=sequence + 1,
                role=MessageRole.USER,
                blocks=(
                    ToolResultBlock(
                        tool_call_id=f"call-{index}",
                        status=status,
                        content=content,
                        evidence_ids=(),
                        persisted_output=False,
                    ),
                ),
                created_at=NOW,
            )
        )


def tool_pair(call_id: str, *, content: str, status: ToolCallStatus) -> MessageGroup:
    return MessageGroup(
        (
            TranscriptMessage(
                agent_run_id="run-1",
                sequence=1,
                role=MessageRole.ASSISTANT,
                blocks=(
                    ToolUseBlock(
                        tool_call_id=call_id, tool_name="logs.query", arguments={}
                    ),
                ),
                created_at=NOW,
            ),
            TranscriptMessage(
                agent_run_id="run-1",
                sequence=2,
                role=MessageRole.USER,
                blocks=(
                    ToolResultBlock(
                        tool_call_id=call_id,
                        status=status,
                        content=content,
                        evidence_ids=(),
                        persisted_output=False,
                    ),
                ),
                created_at=NOW,
            ),
        )
    )


# -- brief Step 1 tests --------------------------------------------------------


def test_large_tool_result_is_persisted_before_active_preview(store) -> None:
    evidence = persist_tool_output(store, content="x" * 250_000)
    append_budgeted_tool_result(store, evidence=evidence, preview_chars=2_000)
    active = manager(store, max_input_tokens=20_000).build(run(), investigation(), schemas())
    result = find_tool_result(active.messages, "call-1")
    assert result.persisted_output is True
    assert len(result.content) < 10_000
    assert store.get_evidence(evidence.evidence_id).content_redacted == "x" * 250_000
    assert store.list_transcript_messages("run-1")[2].blocks[0] == result


def test_budget_counts_system_tools_messages_and_output_reserve(store) -> None:
    active = manager(
        store, context_window=16_000, max_output_tokens=2_000, reserve_tokens=1_000
    ).build(run(), investigation(), large_schemas())
    assert active.budget.max_input_tokens == 13_000
    assert active.budget.input_tokens <= 13_000
    assert active.budget.system_tokens > 0
    assert active.budget.tool_tokens > 0
    assert active.budget.message_tokens > 0


def test_rendered_system_prompt_contributes_to_budget(store) -> None:
    """Task 4: a non-empty rendered prompt raises ``ContextBudget.system_tokens``.

    The manager-backed budget must count the prompt the provider will actually
    send, not the empty ``ContextBudgetPolicy.system_prompt`` fallback.  Two
    managers are built over the same run/investigation: one with a renderer
    returning a known non-empty string and one without (the default empty
    policy).  The renderer manager's ``system_tokens`` is larger by exactly the
    renderer's counted tokens, net of the estimator's 1-token floor for the
    baseline empty string.
    """
    known_prompt = "你是 IncidentLens 的受限事故调查规划器。请只返回一个 JSON 对象。" * 5
    run_obj = run()
    investigation_obj = investigation()
    schema_set = schemas()

    baseline = manager(store, system_prompt="").build(
        run_obj, investigation_obj, schema_set
    )

    rendered = AgentContextManager(
        store,
        policy=ContextBudgetPolicy(),
        now=lambda: NOW,
        system_prompt_renderer=(
            lambda run, investigation, tool_schemas, memory: known_prompt
        ),
    ).build(run_obj, investigation_obj, schema_set)

    extra = rendered.budget.system_tokens - baseline.budget.system_tokens
    assert extra == ConservativeTokenEstimator().count_text(known_prompt) - 1


def test_materialization_preserves_all_tool_results_without_context_pressure(
    store,
) -> None:
    """The micro-compaction retention count is not an always-on history window."""
    seed_many_message_groups(store, count=12, content_width=800)

    active = manager(store).build(
        run(), investigation(), schemas()
    )

    results = {
        block.tool_call_id: block
        for message in active.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    }
    assert len(results) == 12
    assert all(block.content.startswith("result ") for block in results.values())
    assert active.budget.input_tokens < active.budget.max_input_tokens


def test_materialization_does_not_snip_small_groups_without_token_pressure(
    store,
) -> None:
    """A message-count threshold alone must not discard an in-budget transcript."""
    seed_many_message_groups(store, count=60, content_width=20)

    active = manager(store).build(
        run(), investigation(), schemas()
    )

    result_ids = {
        block.tool_call_id
        for message in active.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    }
    assert result_ids == {f"call-{index}" for index in range(60)}
    assert active.budget.input_tokens < active.budget.max_input_tokens


# -- deterministic transforms --------------------------------------------------


def test_tool_result_budget_truncates_oversized_results() -> None:
    groups = (
        tool_pair("call-1", content="x" * 20_000, status=ToolCallStatus.SUCCEEDED),
    )
    budgeted = tool_result_budget(groups, max_chars=10_000)
    result = find_tool_result(flatten(budgeted), "call-1")
    assert result.persisted_output is True
    assert result.content.startswith("x" * 10_000)
    assert "truncated" in result.content


def test_tool_result_budget_is_group_atomic() -> None:
    groups = (
        tool_pair("call-1", content="x" * 20_000, status=ToolCallStatus.SUCCEEDED),
        tool_pair("call-2", content="short", status=ToolCallStatus.SUCCEEDED),
    )
    budgeted = tool_result_budget(groups, max_chars=1_000)
    assert_tool_pairs_are_complete(flatten(budgeted))
    blocks = [
        block
        for message in flatten(budgeted)
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    ]
    assert len(blocks) == 2
    oversized = next(block for block in blocks if block.tool_call_id == "call-1")
    assert oversized.persisted_output is True
    assert oversized.content.startswith("x" * 1_000)
    kept = next(block for block in blocks if block.tool_call_id == "call-2")
    assert kept.persisted_output is False
    assert kept.content == "short"


def test_micro_compact_stubs_only_results_older_than_time_threshold() -> None:
    groups = tuple(
        tool_pair(f"call-{index}", content="x" * 500, status=ToolCallStatus.SUCCEEDED)
        for index in range(6)
    )
    groups = tuple(
        MessageGroup(
            tuple(
                message.model_copy(
                    update={
                        "created_at": NOW - timedelta(minutes=61)
                        if index < 4
                        else NOW
                    }
                )
                for message in group.messages
            )
        )
        for index, group in enumerate(groups)
    )
    compacted = micro_compact(groups, now=NOW, minimum_age_seconds=3_600)
    messages = flatten(compacted)
    assert_tool_pairs_are_complete(messages)
    results = [
        block
        for message in messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    ]
    stubbed = [block for block in results if "persisted in EvidenceStore" in block.content]
    assert len(stubbed) == 4
    assert all(block.persisted_output for block in stubbed)
    fresh = [block for block in results if block.tool_call_id in {"call-4", "call-5"}]
    assert all(block.content == "x" * 500 for block in fresh)


def test_micro_compact_never_discards_short_configuration_results() -> None:
    groups = tuple(
        tool_pair(
            f"call-{index}",
            content=f"DB_PORT={5432 + index}",
            status=ToolCallStatus.SUCCEEDED,
        )
        for index in range(8)
    )

    old_groups = tuple(
        MessageGroup(
            tuple(
                message.model_copy(update={"created_at": NOW - timedelta(hours=2)})
                for message in group.messages
            )
        )
        for group in groups
    )
    compacted = micro_compact(
        old_groups, now=NOW, minimum_age_seconds=3_600
    )

    results = [
        block
        for message in flatten(compacted)
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    ]
    assert [block.content for block in results] == [
        f"DB_PORT={5432 + index}" for index in range(8)
    ]


def test_micro_compact_uses_reacquisition_stub_for_reproducible_result() -> None:
    group = MessageGroup(
        (
            TranscriptMessage(
                agent_run_id="run-1",
                sequence=1,
                role=MessageRole.ASSISTANT,
                blocks=(
                    ToolUseBlock(
                        tool_call_id="call-1",
                        tool_name="log_query",
                        arguments={"service_name": "orders"},
                    ),
                ),
                created_at=NOW,
            ),
            TranscriptMessage(
                agent_run_id="run-1",
                sequence=2,
                role=MessageRole.USER,
                blocks=(
                        ToolResultBlock(
                            tool_call_id="call-1",
                            status=ToolCallStatus.SUCCEEDED,
                            content="current log observation " + "x" * 200,
                        evidence_ids=("ev-1",),
                    ),
                ),
                created_at=NOW,
            ),
        )
    )
    old_group = MessageGroup(
        tuple(
            message.model_copy(update={"created_at": NOW - timedelta(hours=2)})
            for message in group.messages
        )
    )
    result = find_tool_result(
        flatten(micro_compact((old_group,), now=NOW, minimum_age_seconds=3_600)),
        "call-1",
    )
    assert "reacquire with log_query" in result.content
    assert "evidence_read" not in result.content


def test_micro_compact_uses_evidence_read_only_for_immutable_result() -> None:
    group = MessageGroup(
        (
            TranscriptMessage(
                agent_run_id="run-1",
                sequence=1,
                role=MessageRole.ASSISTANT,
                blocks=(
                    ToolUseBlock(
                        tool_call_id="call-1",
                        tool_name="file_edit",
                        arguments={"path": "/etc/orders.env"},
                    ),
                ),
                created_at=NOW,
            ),
            TranscriptMessage(
                agent_run_id="run-1",
                sequence=2,
                role=MessageRole.USER,
                blocks=(
                        ToolResultBlock(
                            tool_call_id="call-1",
                            status=ToolCallStatus.SUCCEEDED,
                            content="pre-change snapshot " + "x" * 200,
                        evidence_ids=("ev-immutable",),
                    ),
                ),
                created_at=NOW,
            ),
        )
    )
    old_group = MessageGroup(
        tuple(
            message.model_copy(update={"created_at": NOW - timedelta(hours=2)})
            for message in group.messages
        )
    )
    result = find_tool_result(
        flatten(micro_compact((old_group,), now=NOW, minimum_age_seconds=3_600)),
        "call-1",
    )
    assert "evidence_read" in result.content
    assert "only if source is gone" in result.content


# -- deterministic session memory under context pressure -----------------------


def test_oversized_context_builds_deterministic_memory_and_boundary(store) -> None:
    seed_many_message_groups(store, count=60, content_width=300)
    active = manager(store, max_input_tokens=3_000).build(run(), investigation(), schemas())
    assert active.memory is not None
    assert active.memory.through_transcript_sequence > 0
    boundary = store.get_latest_compact_boundary("run-1")
    assert boundary is not None
    assert boundary.through_sequence == active.memory.through_transcript_sequence
    assert boundary.memory_revision == active.memory.revision
    assert active.budget.input_tokens <= active.budget.max_input_tokens
    # every replayed transcript message is post-boundary; the header is index 0
    for message in active.messages[1:]:
        assert message.sequence > active.memory.through_transcript_sequence


def test_no_memory_means_no_trim_even_when_over_budget(store) -> None:
    """Groups are never dropped when no Session Memory exists to summarize them.

    A degenerate input budget (smaller than the system/tool/header overhead)
    makes the coverage scan unable to compact: no boundary candidate fits, so
    no memory revision is built.  The active context is then over budget but
    must still replay every recent group -- trimming would silently lose
    history no memory records.
    """
    seed_many_message_groups(store, count=10, content_width=200)
    active = manager(
        store, context_window=8_000, max_output_tokens=7_900, reserve_tokens=0
    ).build(run(), investigation(), schemas())
    assert active.memory is None
    assert store.get_latest_compact_boundary("run-1") is None
    assert active.budget.input_tokens > active.budget.max_input_tokens
    # every seeded tool pair is still replayed -- no trimming without memory
    assert_tool_pairs_are_complete(active.messages)
    assert {f"call-{index}" for index in range(10)} <= {
        block.tool_call_id
        for message in active.messages
        for block in message.blocks
        if isinstance(block, ToolUseBlock)
    }


def test_existing_memory_never_authorizes_dropping_uncovered_tail_groups(
    store,
) -> None:
    """A protected post-boundary group blocks coverage, not tail preservation."""
    store.append_transcript_message(
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=1,
            role=MessageRole.USER,
            blocks=(TextBlock(text="durable objective"),),
            created_at=NOW,
        )
    )
    memory = SessionMemory(
        memory_id="mem-run-1-1",
        agent_run_id="run-1",
        investigation_id="inv-1",
        revision=1,
        through_round=0,
        through_transcript_sequence=1,
        objective="checkout requests return 502",
        created_at=NOW,
    )
    store.append_session_memory(memory)
    store.append_compact_boundary(
        CompactBoundary(
            agent_run_id="run-1",
            through_sequence=1,
            memory_revision=1,
            summary="initial compact",
            created_at=NOW,
        )
    )
    groups = (
        tool_pair("protected", content="failed mutation", status=ToolCallStatus.FAILED),
        *(
            tool_pair(
                f"tail-{index}",
                content="x" * 600,
                status=ToolCallStatus.SUCCEEDED,
            )
            for index in range(8)
        ),
    )
    sequence = 2
    for group in groups:
        for message in group.messages:
            store.append_transcript_message(
                message.model_copy(update={"sequence": sequence})
            )
            sequence += 1

    active = manager(
        store, max_input_tokens=500, micro_compact_after_seconds=3_600
    ).build(
        run(), investigation(), schemas()
    )

    result_ids = {
        block.tool_call_id
        for message in active.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    }
    assert result_ids == {"protected", *(f"tail-{index}" for index in range(8))}
    assert active.budget.input_tokens > active.budget.max_input_tokens


def test_deterministic_memory_captures_durable_work_state(store) -> None:
    store.replace_todos(
        "run-1",
        (
            TodoItem(
                todo_id="inspect",
                content="inspect order logs",
                status=TodoStatus.IN_PROGRESS,
                updated_at=NOW,
            ),
            TodoItem(
                todo_id="verify",
                content="verify root cause",
                status=TodoStatus.PENDING,
                updated_at=NOW,
            ),
        ),
    )
    seed_many_message_groups(store, count=60, content_width=300)
    active = manager(store, max_input_tokens=3_000).build(run(), investigation(), schemas())
    assert active.memory is not None
    assert active.memory.objective == "checkout requests return 502"
    assert active.memory.todos == (
        "[in_progress] inspect order logs",
        "[pending] verify root cause",
    )
    assert active.memory.next_actions == ("inspect order logs", "verify root cause")
    assert [item.todo_id for item in active.todos] == ["inspect", "verify"]


def test_memory_converges_then_is_reused(store) -> None:
    seed_many_message_groups(store, count=60, content_width=300)
    manager_ = manager(store, max_input_tokens=3_000)
    first = manager_.build(run(), investigation(), schemas())
    assert first.memory is not None
    # The boundary advances across builds until the replayed tail fits.
    previous = first
    for _ in range(20):
        current = manager_.build(run(), investigation(), schemas())
        if current.memory == previous.memory and (
            current.budget.input_tokens <= current.budget.max_input_tokens
        ):
            break
        previous = current
    stable = manager_.build(run(), investigation(), schemas())
    assert stable.memory == previous.memory
    assert stable.budget.input_tokens <= stable.budget.max_input_tokens
    assert len(store.list_session_memories("run-1")) == previous.memory.revision


def test_header_carries_memory_and_todos(store) -> None:
    store.replace_todos(
        "run-1",
        (
            TodoItem(
                todo_id="inspect",
                content="inspect order logs",
                status=TodoStatus.IN_PROGRESS,
                updated_at=NOW,
            ),
        ),
    )
    seed_many_message_groups(store, count=60, content_width=300)
    active = manager(store, max_input_tokens=3_000).build(run(), investigation(), schemas())
    assert active.memory is not None
    header = active.messages[0]
    assert header.role is MessageRole.USER
    text = header.blocks[0].text
    assert "Session memory" in text
    assert "inspect order logs" in text
    assert "checkout requests return 502" in text


def _evidence_run(*refs: EvidenceReference) -> AgentRun:
    return run().model_copy(update={"evidence": refs})


def _child_report(*, summary: str) -> ChildReport:
    return ChildReport(
        agent_run_id="child-1",
        parent_run_id="run-1",
        status=ChildReportStatus.COMPLETE,
        summary=summary,
        findings=("db pool exhausted",),
        stop_reason=StopReason.COMPLETED,
        created_at=NOW,
    )


def test_header_restores_plan_evidence_and_child_reports(store) -> None:
    store.replace_todos(
        "run-1",
        (
            TodoItem(
                todo_id="inspect",
                content="inspect order logs",
                status=TodoStatus.IN_PROGRESS,
                updated_at=NOW,
            ),
        ),
    )
    active = manager(store).build(
        _evidence_run(
            EvidenceReference(
                evidence_id="ev-1", operation_id="op-1", summary="timeout burst in order logs"
            )
        ),
        investigation(),
        schemas(),
        child_reports=(_child_report(summary="db connection pool exhausted"),),
    )
    text = active.messages[0].blocks[0].text
    # The fixed restoration sections: plan, evidence refs, child reports.
    assert "Work plan:" in text
    assert "- [in_progress] inspect order logs" in text
    assert "Evidence collected (recent):" in text
    assert "ev-1" in text
    assert "Latest child reports:" in text
    assert "db connection pool exhausted" in text
    # The instruction to keep the plan current is part of the fixed header.
    assert "create or update the work plan" in text
    # Restoration attachments precede the session memory section.
    assert text.index("Work plan:") < text.index("Evidence collected (recent):")
    assert text.index("Evidence collected (recent):") < text.index("Latest child reports:")


def test_header_places_restoration_before_session_memory(store) -> None:
    store.replace_todos(
        "run-1",
        (
            TodoItem(
                todo_id="inspect",
                content="inspect order logs",
                status=TodoStatus.IN_PROGRESS,
                updated_at=NOW,
            ),
        ),
    )
    seed_many_message_groups(store, count=60, content_width=300)
    active = manager(store, max_input_tokens=3_000).build(
        _evidence_run(
            EvidenceReference(
                evidence_id="ev-1", operation_id="op-1", summary="timeout burst"
            )
        ),
        investigation(),
        schemas(),
        child_reports=(_child_report(summary="db connection pool exhausted"),),
    )
    assert active.memory is not None
    text = active.messages[0].blocks[0].text
    assert "Session memory" in text
    assert text.index("Work plan:") < text.index("Session memory")
    assert text.index("Evidence collected (recent):") < text.index("Session memory")
    assert text.index("Latest child reports:") < text.index("Session memory")
    assert text.index("create or update the work plan") < text.index("Session memory")


def test_header_restores_recent_file_content_after_compaction(store) -> None:
    content = "DB_PORT=55432\nMODE=canary\n"
    evidence = EvidenceStore(store.connection_factory).create(
        EvidenceRef(
            evidence_ref_id="ev-config-page",
            incident_id="inc-1",
            evidence_kind=EvidenceKind.FILE_SNAPSHOT,
            agent_run_id="run-1",
            project_id="checkout",
            target_id="prod-a",
            service_name="orders",
            source_ref="/opt/orders/config/order-canary.env",
            content_redacted=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            redaction_summary={},
            metadata={
                "operation": "host_read",
                "scope": "host",
                "read_mode": "lines",
                "source_sha256": "a" * 64,
            },
            created_at=NOW,
            created_by="agent",
        )
    )
    store.append_transcript_message(
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=1,
            role=MessageRole.USER,
            blocks=(TextBlock(text="inspect canary configuration"),),
            created_at=NOW,
        )
    )
    memory = SessionMemory(
        memory_id="mem-run-1-1",
        agent_run_id="run-1",
        investigation_id="inv-1",
        revision=1,
        through_round=1,
        through_transcript_sequence=1,
        objective="repair checkout failures",
        evidence_ids=(evidence.evidence_ref_id,),
        created_at=NOW,
    )
    store.append_session_memory(memory)
    store.append_compact_boundary(
        CompactBoundary(
            agent_run_id="run-1",
            through_sequence=1,
            memory_revision=1,
            summary="semantic compact",
            created_at=NOW,
        )
    )

    active = manager(store).build(run(), investigation(), schemas())

    header = active.messages[0].blocks[0].text
    assert "Recent file contents restored after compaction:" in header
    assert "/opt/orders/config/order-canary.env" in header
    assert "evidence=ev-config-page" in header
    assert "DB_PORT=55432" in header
    assert header.index("Recent file contents restored after compaction:") < header.index(
        "Session memory"
    )


def test_post_compact_restoration_keeps_multiple_pages_of_same_file(store) -> None:
    evidence_store = EvidenceStore(store.connection_factory)
    evidence_ids: list[str] = []
    for index, (start, end, content) in enumerate(
        ((1, 2, "alpha\nbeta\n"), (3, 4, "ROOT_CAUSE=wrong-port\nomega\n")),
        start=1,
    ):
        ref = evidence_store.create(
            EvidenceRef(
                evidence_ref_id=f"ev-config-page-{index}",
                incident_id="inc-1",
                evidence_kind=EvidenceKind.FILE_SNAPSHOT,
                agent_run_id="run-1",
                project_id="checkout",
                target_id="prod-a",
                service_name="orders",
                source_ref="/opt/orders/config/order-canary.env",
                content_redacted=content,
                content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                redaction_summary={},
                metadata={
                    "operation": "host_read",
                    "scope": "host",
                    "read_mode": "lines",
                    "start_line": str(start),
                    "end_line": str(end),
                    "source_sha256": "a" * 64,
                },
                created_at=NOW,
                created_by="agent",
            )
        )
        evidence_ids.append(ref.evidence_ref_id)
    store.append_transcript_message(
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=1,
            role=MessageRole.USER,
            blocks=(TextBlock(text="inspect all relevant config pages"),),
            created_at=NOW,
        )
    )
    memory = SessionMemory(
        memory_id="mem-run-1-pages",
        agent_run_id="run-1",
        investigation_id="inv-1",
        revision=1,
        through_round=1,
        through_transcript_sequence=1,
        objective="repair checkout failures",
        evidence_ids=tuple(evidence_ids),
        created_at=NOW,
    )
    store.append_session_memory(memory)
    store.append_compact_boundary(
        CompactBoundary(
            agent_run_id="run-1",
            through_sequence=1,
            memory_revision=1,
            summary="semantic compact",
            created_at=NOW,
        )
    )

    header = manager(store).build(run(), investigation(), schemas()).messages[0].blocks[0].text

    assert "alpha\nbeta" in header
    assert "ROOT_CAUSE=wrong-port\nomega" in header
    assert header.index("alpha\nbeta") < header.index("ROOT_CAUSE=wrong-port")
    assert "ev-config-page-1" in header
    assert "ev-config-page-2" in header


def test_restored_file_truncation_including_marker_stays_within_token_cap(store) -> None:
    context_manager = manager(store)

    truncated = context_manager._truncate_text_to_tokens("x" * 20_000, 5_000)

    assert truncated.endswith("...[restored file truncated]")
    assert ConservativeTokenEstimator().count_text(truncated) <= 5_000


def test_child_header_includes_delegated_task(store) -> None:
    from incidentlens_control_plane.investigation.types import (
        DelegatedTaskPackage,
    )

    parent = run()
    child = AgentRun(
        agent_run_id="child-1",
        investigation_id="inv-1",
        parent_run_id="run-1",
        kind=AgentRunKind.CHILD,
        scope=AgentScope(
            project_id="checkout",
            target_id="prod-a",
            scope=LogScope.CONTAINER,
            service_name="orders",
            container_name="orders-1",
        ),
        status=AgentRunStatus.RUNNING,
        budget=AgentBudget(),
        usage=UsageCounters(),
        created_at=NOW,
        updated_at=NOW,
    )
    store.create_agent_run(parent)
    store.create_agent_run(child)
    store.create_delegated_task(
        DelegatedTaskPackage(
            child_run_id=child.agent_run_id,
            parent_run_id=parent.agent_run_id,
            investigation_id="inv-1",
            task_prompt="inspect the orders container timeout",
            scope=child.scope,
            budget=child.budget,
        ),
        now=NOW,
    )
    active = manager(store).build(child, investigation(), schemas())
    assert "inspect the orders container timeout" in active.messages[0].blocks[0].text


# -- conservative estimator ----------------------------------------------------


def test_calibration_only_lowers_chars_per_token() -> None:
    estimator = ConservativeTokenEstimator(chars_per_token=4.0)
    text = "x" * 100
    before = estimator.count_text(text)
    assert before == 25
    estimator.calibrate(actual_input_tokens=50, estimated_input_tokens=before)
    after = estimator.count_text(text)
    assert after == 50
    assert after > before
    # an estimate that already covers actual usage is never raised optimistically
    estimator.calibrate(actual_input_tokens=10, estimated_input_tokens=after)
    assert estimator.count_text(text) == after


# -- compaction policy limits --------------------------------------------------


def test_context_policy_rejects_invalid_compaction_limits() -> None:
    with pytest.raises(ValueError, match="compact_max_failures"):
        ContextBudgetPolicy(compact_max_failures=0)
    with pytest.raises(ValueError, match="reactive_keep_recent_groups"):
        ContextBudgetPolicy(reactive_keep_recent_groups=0)


# -- pressure-driven semantic compaction --------------------------------------


class _RecordingCompactor:
    """A compactor that records every request and returns a scripted memory.

    The returned memory is aligned to the request (revision and boundary), so
    a full-transcript semantic compact commits a fresh revision instead of
    colliding on a stale one.
    """

    def __init__(self, memory: SessionMemory) -> None:
        self._memory = memory
        self.requests: list[CompactionRequest] = []

    async def compact(self, request: CompactionRequest) -> SessionMemory:
        self.requests.append(request)
        revision = (
            (request.prior_memory.revision + 1)
            if request.prior_memory is not None
            else 1
        )
        return self._memory.model_copy(
            update={
                "revision": revision,
                "memory_id": f"mem-{request.agent_run_id}-{revision}",
                "through_transcript_sequence": max(
                    request.through_sequence,
                    self._memory.through_transcript_sequence,
                ),
            }
        )


class _FailingCompactor:
    """A compactor that always rejects, recording how many times it was called."""

    def __init__(self) -> None:
        self.calls = 0

    async def compact(self, request: CompactionRequest) -> SessionMemory:
        self.calls += 1
        raise CompactionRejected("scripted compactor failure")


@pytest.fixture
def recording_compactor() -> _RecordingCompactor:
    """A semantic compactor that preserves the incident work state."""
    return _RecordingCompactor(
        SessionMemory(
            memory_id="mem-run-1-1",
            agent_run_id="run-1",
            investigation_id="inv-1",
            revision=1,
            through_round=0,
            through_transcript_sequence=1,
            objective="checkout requests return 502",
            immutable_observations=("pre-change target sha256 " + "a" * 64,),
            todos=("[pending] verify root cause",),
            evidence_ids=("ev-hash",),
            created_at=NOW,
        )
    )


@pytest.fixture
def pressure_manager(store: InvestigationStore, recording_compactor) -> AgentContextManager:
    """A manager whose semantic pressure threshold sits far below the ceiling.

    ``semantic_compact_at_fraction=0.1`` means a modest seeded transcript
    crosses the pressure band (10k of 107k tokens) without ever tripping the
    deterministic over-``max_input_tokens`` path, so ``prepare`` exercises the
    semantic compactor rather than the deterministic memory fallback.
    """
    return AgentContextManager(
        store,
        policy=ContextBudgetPolicy(
            context_window=128_000,
            max_output_tokens=8_000,
            reserve_tokens=13_000,
            semantic_compact_at_fraction=0.1,
        ),
        compactor=recording_compactor,
        now=lambda: NOW,
    )


def seed_large_transcript_with_target_hash(
    store: InvestigationStore, run: AgentRun, *, sha256: str
) -> AgentRun:
    """Append a large transcript plus an immutable pre-change observation.

    The immutable ``file_edit`` pair records ``sha256`` as the observed target
    file hash and attaches matching owned evidence to the run, so whichever
    memory path summarizes the transcript still surfaces the hash.
    """
    seed_many_message_groups(store, count=60, content_width=500)
    call = ToolCall(
        tool_call_id="call-hash",
        agent_run_id="run-1",
        tool_name="file_edit",
        status=ToolCallStatus.SUCCEEDED,
        idempotency_key="call-hash",
        planned_at=NOW,
        finished_at=NOW,
    )
    store.create_tool_call(call)
    evidence = EvidenceReference(
        evidence_id="ev-hash",
        operation_id="call-hash",
        summary=f"pre-change target sha256 {sha256}",
    )
    store.append_transcript_message(
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=121,
            role=MessageRole.ASSISTANT,
            blocks=(
                ToolUseBlock(
                    tool_call_id="call-hash",
                    tool_name="file_edit",
                    arguments={"path": "/etc/orders.env", "expected_sha256": sha256},
                ),
            ),
            created_at=NOW,
        )
    )
    store.append_transcript_message(
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=122,
            role=MessageRole.USER,
            blocks=(
                ToolResultBlock(
                    tool_call_id="call-hash",
                    status=ToolCallStatus.SUCCEEDED,
                    content=f"pre-change sha256 {sha256} persisted",
                    evidence_ids=(evidence.evidence_id,),
                    persisted_output=True,
                ),
            ),
            created_at=NOW,
        )
    )
    return run.model_copy(update={"evidence": (evidence,)})


def test_semantic_compaction_is_not_requested_below_pressure(
    store: InvestigationStore, pressure_manager: AgentContextManager, recording_compactor
) -> None:
    active = pressure_manager.build(run(), investigation(), schemas())
    assert active.budget.input_tokens < active.budget.semantic_compact_at_tokens
    assert recording_compactor.requests == []


@pytest.mark.asyncio
async def test_pressure_compaction_preserves_work_state(
    store: InvestigationStore, pressure_manager: AgentContextManager, recording_compactor
) -> None:
    seeded = seed_large_transcript_with_target_hash(store, run(), sha256="a" * 64)
    active = await pressure_manager.prepare(seeded, investigation(), schemas())
    assert active.memory is not None
    assert active.memory.objective == investigation().symptom
    assert "a" * 64 in " ".join(active.memory.immutable_observations)
    assert active.memory.todos
    assert active.budget.input_tokens <= active.budget.max_input_tokens
    # Pressing the threshold must actually invoke the semantic compactor.
    assert recording_compactor.requests


@pytest.mark.asyncio
async def test_prepare_falls_back_without_boundary_advance_on_semantic_failure(
    store: InvestigationStore,
) -> None:
    """A failed semantic compact returns an in-budget context, never advancing
    the previous boundary (the failure breaker still counts the attempt)."""
    failing = _FailingCompactor()
    manager_ = AgentContextManager(
        store,
        policy=ContextBudgetPolicy(
            context_window=128_000,
            max_output_tokens=8_000,
            reserve_tokens=13_000,
            semantic_compact_at_fraction=0.1,
        ),
        compactor=failing,
        now=lambda: NOW,
    )
    seeded = seed_large_transcript_with_target_hash(store, run(), sha256="a" * 64)
    active = await manager_.prepare(seeded, investigation(), schemas())
    assert failing.calls == 1
    assert store.get_latest_compact_boundary("run-1") is None
    assert active.memory is None
    assert active.budget.input_tokens <= active.budget.max_input_tokens
    state = store.get_compaction_state("run-1")
    assert state is not None and state.consecutive_failures == 1
