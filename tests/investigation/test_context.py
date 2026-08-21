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
from datetime import UTC, datetime

import pytest
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.evidence.types import EvidenceKind, EvidenceRef
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
    EvidenceReference,
    Investigation,
    InvestigationBudget,
    MessageRole,
    StopReason,
    TextBlock,
    TodoItem,
    TodoStatus,
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
    max_groups: int | None = None,
    tool_result_budget_chars: int | None = None,
    keep_recent: int | None = None,
    system_prompt: str | None = None,
) -> AgentContextManager:
    """Build a manager with explicit budget/compaction overrides."""
    policy = ContextBudgetPolicy(
        context_window=context_window if context_window is not None else 128_000,
        max_output_tokens=max_output_tokens if max_output_tokens is not None else 8_000,
        reserve_tokens=reserve_tokens if reserve_tokens is not None else 13_000,
        max_message_groups=max_groups if max_groups is not None else 50,
        tool_result_budget_chars=(
            tool_result_budget_chars
            if tool_result_budget_chars is not None
            else 200_000
        ),
        keep_recent_tool_results=keep_recent if keep_recent is not None else 3,
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


def test_snip_never_splits_tool_pair(store) -> None:
    seed_many_message_groups(store, count=60)
    active = manager(store, max_groups=50).build(run(), investigation(), schemas())
    assert_tool_pairs_are_complete(active.messages)


def test_budget_counts_system_tools_messages_and_output_reserve(store) -> None:
    active = manager(
        store, context_window=16_000, max_output_tokens=2_000, reserve_tokens=1_000
    ).build(run(), investigation(), large_schemas())
    assert active.budget.max_input_tokens == 13_000
    assert active.budget.input_tokens <= 13_000
    assert active.budget.system_tokens > 0
    assert active.budget.tool_tokens > 0
    assert active.budget.message_tokens > 0


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


def test_micro_compact_stubs_old_succeeded_results_keeps_recent() -> None:
    groups = tuple(
        tool_pair(f"call-{index}", content="x" * 500, status=ToolCallStatus.SUCCEEDED)
        for index in range(6)
    )
    compacted = micro_compact(groups, keep_recent=2)
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
    recent = [block for block in results if block.tool_call_id in {"call-4", "call-5"}]
    assert all(block.content == "x" * 500 for block in recent)


def test_snip_keeps_protected_failed_result(store) -> None:
    seed_many_message_groups(store, count=60, content_width=200, fail_first=True)
    active = manager(store, max_groups=10).build(run(), investigation(), schemas())
    assert_tool_pairs_are_complete(active.messages)
    failed = [
        block
        for message in active.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock) and block.status is ToolCallStatus.FAILED
    ]
    assert len(failed) == 1
    assert failed[0].tool_call_id == "call-0"


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
                        content="current log observation",
                        evidence_ids=("ev-1",),
                    ),
                ),
                created_at=NOW,
            ),
        )
    )
    result = find_tool_result(flatten(micro_compact((group,), keep_recent=0)), "call-1")
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
                        content="pre-change snapshot",
                        evidence_ids=("ev-immutable",),
                    ),
                ),
                created_at=NOW,
            ),
        )
    )
    result = find_tool_result(flatten(micro_compact((group,), keep_recent=0)), "call-1")
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
