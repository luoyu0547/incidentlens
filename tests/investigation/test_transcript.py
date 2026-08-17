"""Transcript persistence, grouping and work-plan tests.

The transcript is the append-only, model-visible message history for one agent
run. These tests cover the persistence contract (unique ``(agent_run_id,
sequence)``), the at-most-one-in-progress work-plan rule, and the
tool-use/tool-result pairing invariants that compaction, retry and recovery
depend on.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from incidentlens_control_plane.investigation.state_machine import ToolCallStatus
from incidentlens_control_plane.investigation.store import (
    InvestigationStore,
    TranscriptConflict,
)
from incidentlens_control_plane.investigation.transcript import (
    MessageGroup,
    TranscriptService,
    UnpairedToolMessage,
    group_messages,
)
from incidentlens_control_plane.investigation.types import (
    MessageRole,
    TextBlock,
    TodoItem,
    TodoStatus,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
)

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)


def make_store(tmp_path) -> InvestigationStore:
    store = InvestigationStore(lambda: sqlite3.connect(tmp_path / "investigations.db"))
    store.migrate()
    return store


def text_message(sequence: int, text: str) -> TranscriptMessage:
    return TranscriptMessage(
        agent_run_id="run-1",
        sequence=sequence,
        role=MessageRole.USER,
        blocks=(TextBlock(text=text),),
        created_at=NOW,
    )


def tool_pair(
    use_sequence: int, call_id: str = "call-1"
) -> tuple[TranscriptMessage, TranscriptMessage]:
    use = TranscriptMessage(
        agent_run_id="run-1",
        sequence=use_sequence,
        role=MessageRole.ASSISTANT,
        blocks=(
            ToolUseBlock(
                tool_call_id=call_id, tool_name="registry_info", arguments={}
            ),
        ),
        created_at=NOW,
    )
    result = TranscriptMessage(
        agent_run_id="run-1",
        sequence=use_sequence + 1,
        role=MessageRole.USER,
        blocks=(
            ToolResultBlock(
                tool_call_id=call_id,
                status=ToolCallStatus.SUCCEEDED,
                content="ok",
            ),
        ),
        created_at=NOW,
    )
    return use, result


# -- transcript persistence ---------------------------------------------------


def test_transcript_is_append_only_and_ordered(tmp_path) -> None:
    store = make_store(tmp_path)
    first = TranscriptMessage(
        agent_run_id="run-1",
        sequence=1,
        role=MessageRole.USER,
        blocks=(TextBlock(text="inspect checkout failures"),),
        created_at=NOW,
    )
    second = TranscriptMessage(
        agent_run_id="run-1",
        sequence=2,
        role=MessageRole.ASSISTANT,
        blocks=(
            ToolUseBlock(tool_call_id="call-1", tool_name="registry_info", arguments={}),
        ),
        created_at=NOW,
    )
    store.append_transcript_message(first)
    store.append_transcript_message(second)
    assert store.list_transcript_messages("run-1") == (first, second)
    with pytest.raises(TranscriptConflict):
        store.append_transcript_message(second)


def test_list_transcript_messages_is_empty_for_unknown_run(tmp_path) -> None:
    store = make_store(tmp_path)
    assert store.list_transcript_messages("run-1") == ()


# -- work plan ----------------------------------------------------------------


def test_work_plan_has_at_most_one_in_progress_item(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="at most one"):
        store.replace_todos(
            "run-1",
            (
                TodoItem(
                    todo_id="one",
                    content="inspect logs",
                    status=TodoStatus.IN_PROGRESS,
                    updated_at=NOW,
                ),
                TodoItem(
                    todo_id="two",
                    content="check database",
                    status=TodoStatus.IN_PROGRESS,
                    updated_at=NOW,
                ),
            ),
        )


# -- grouping / pairing -------------------------------------------------------


def test_group_messages_groups_each_single_message() -> None:
    messages = (text_message(1, "first"), text_message(2, "second"))
    assert group_messages(messages) == (
        MessageGroup((messages[0],)),
        MessageGroup((messages[1],)),
    )


def test_group_messages_pairs_tool_use_with_matching_result() -> None:
    use, result = tool_pair(1)
    assert group_messages((use, result)) == (MessageGroup((use, result)),)


def test_group_messages_mixes_single_messages_and_pairs() -> None:
    first = text_message(1, "hello")
    use, result = tool_pair(2)
    last = text_message(4, "done")
    groups = group_messages((first, use, result, last))
    assert groups == (
        MessageGroup((first,)),
        MessageGroup((use, result)),
        MessageGroup((last,)),
    )


def test_group_messages_raises_when_tool_use_has_no_following_result() -> None:
    use, _ = tool_pair(1)
    with pytest.raises(UnpairedToolMessage, match="no following result"):
        group_messages((use,))


def test_group_messages_raises_on_mismatched_result_ids() -> None:
    use, _ = tool_pair(1)
    wrong = TranscriptMessage(
        agent_run_id="run-1",
        sequence=2,
        role=MessageRole.USER,
        blocks=(
            ToolResultBlock(
                tool_call_id="call-2",
                status=ToolCallStatus.SUCCEEDED,
                content="ok",
            ),
        ),
        created_at=NOW,
    )
    with pytest.raises(UnpairedToolMessage, match="do not match"):
        group_messages((use, wrong))


# -- TranscriptService --------------------------------------------------------


def test_transcript_service_appends_and_groups_after_sequence(tmp_path) -> None:
    store = make_store(tmp_path)
    service = TranscriptService(store)
    first = text_message(1, "hello")
    use, result = tool_pair(2)
    service.append_message(first)
    service.append_message(use)
    service.append_message(result)

    assert service.group_messages("run-1") == (
        MessageGroup((first,)),
        MessageGroup((use, result)),
    )
    assert service.group_messages("run-1", after=1) == (
        MessageGroup((use, result)),
    )


def test_transcript_service_append_rejects_duplicate_sequence(tmp_path) -> None:
    store = make_store(tmp_path)
    service = TranscriptService(store)
    service.append_message(text_message(1, "hello"))
    with pytest.raises(TranscriptConflict):
        service.append_message(text_message(1, "again"))


def test_transcript_service_after_never_splits_tool_pair(tmp_path) -> None:
    """An ``after`` cut inside a tool pair must keep the pair whole (T1)."""
    store = make_store(tmp_path)
    service = TranscriptService(store)
    service.append_message(text_message(1, "hello"))
    use, result = tool_pair(2)
    service.append_message(use)
    service.append_message(result)

    # after=2 lands between the tool use (seq 2) and its result (seq 3); the
    # pair must survive intact rather than degrade into a lone tool result.
    assert service.group_messages("run-1", after=2) == (
        MessageGroup((use, result)),
    )
    # after=3 drops the whole pair, never a lone tool result.
    assert service.group_messages("run-1", after=3) == ()
