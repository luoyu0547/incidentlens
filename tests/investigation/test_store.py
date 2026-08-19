"""Store tests for investigation persistence, transitions and recovery queries."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest
from incidentlens_control_plane.investigation.state_machine import (
    HYPOTHESIS_STATE_MACHINE,
    INVESTIGATION_TERMINAL,
    AgentRunStatus,
    IllegalTransition,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.store import (
    AgentRound,
    AgentRunNotFound,
    AlreadyExists,
    CheckpointConflict,
    ChildReportReceiptConflict,
    CompactBoundaryConflict,
    ConcurrentModification,
    DelegatedTaskNotFound,
    InvestigationNotFound,
    InvestigationStore,
    ProposalNotFound,
    RoundConflict,
    ToolCallNotFound,
    TranscriptConflict,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    Checkpoint,
    ChildReport,
    ChildReportReceipt,
    ChildReportStatus,
    CompactBoundary,
    CompactionState,
    Conclusion,
    DelegatedTaskPackage,
    Hypothesis,
    HypothesisStatus,
    Investigation,
    InvestigationBudget,
    MessageRole,
    ProviderUsage,
    RegistryProposalStatus,
    RegistryUpdateKind,
    RegistryUpdateProposal,
    SessionMemory,
    StopReason,
    TextBlock,
    TodoItem,
    TodoStatus,
    ToolCall,
    TranscriptMessage,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope
from pydantic import ValidationError

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)


def make_store(tmp_path) -> InvestigationStore:
    store = InvestigationStore(lambda: sqlite3.connect(tmp_path / "investigations.db"))
    store.migrate()
    return store


def make_investigation(**kwargs: object) -> Investigation:
    fields: dict[str, object] = {
        "investigation_id": "inv-1",
        "incident_id": "inc-123",
        "project_id": "proj-1",
        "target_id": "prod-a",
        "service": "orders",
        "symptom": "checkout requests are failing",
        "status": InvestigationStatus.RUNNING,
        "budget": InvestigationBudget(),
        "usage": UsageCounters(),
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(kwargs)
    return Investigation(**fields)


def make_run(
    *,
    agent_run_id: str = "run-1",
    investigation_id: str = "inv-1",
    kind: AgentRunKind = AgentRunKind.PARENT,
    parent_run_id: str | None = None,
    scope: AgentScope | None = None,
    status: AgentRunStatus = AgentRunStatus.RUNNING,
    **kwargs: object,
) -> AgentRun:
    fields: dict[str, object] = {
        "agent_run_id": agent_run_id,
        "investigation_id": investigation_id,
        "kind": kind,
        "parent_run_id": parent_run_id,
        "scope": scope or AgentScope(
            project_id="proj-1", target_id="prod-a", scope=LogScope.HOST
        ),
        "status": status,
        "budget": AgentBudget(),
        "usage": UsageCounters(),
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(kwargs)
    return AgentRun(**fields)


def make_container_child(
    *,
    agent_run_id: str = "child-1",
    parent_run_id: str = "run-1",
    investigation_id: str = "inv-1",
    scope: AgentScope | None = None,
    **kwargs: object,
) -> AgentRun:
    return make_run(
        agent_run_id=agent_run_id,
        investigation_id=investigation_id,
        kind=AgentRunKind.CHILD,
        parent_run_id=parent_run_id,
        scope=scope
        or AgentScope(
            project_id="proj-1",
            target_id="prod-a",
            scope=LogScope.CONTAINER,
            service_name="orders",
            container_name="orders-1",
        ),
        **kwargs,
    )


def make_hypothesis(**kwargs: object) -> Hypothesis:
    fields: dict[str, object] = {
        "hypothesis_id": "hyp-1",
        "agent_run_id": "run-1",
        "summary": "database pool is exhausted",
        "facts": ("orders container reports pool timeout",),
        "inferences": (),
        "unknowns": (),
        "evidence_ids": (),
        "status": HypothesisStatus.PROPOSED,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(kwargs)
    return Hypothesis(**fields)


def make_tool_call(**kwargs: object) -> ToolCall:
    fields: dict[str, object] = {
        "tool_call_id": "tool-1",
        "agent_run_id": "run-1",
        "tool_name": "logs.query",
        "status": ToolCallStatus.PLANNED,
        "idempotency_key": "tool-1",
        "planned_at": NOW,
    }
    fields.update(kwargs)
    return ToolCall(**fields)


def make_proposal(**kwargs: object) -> RegistryUpdateProposal:
    fields: dict[str, object] = {
        "proposal_id": "prop-1",
        "investigation_id": "inv-1",
        "agent_run_id": "run-1",
        "kind": RegistryUpdateKind.CONTAINER_REGISTRATION,
        "discovery_evidence_id": "ev-1",
        "proposed_project_id": "proj-1",
        "proposed_target_id": "prod-a",
        "proposed_service_name": "orders",
        "proposed_container_name": "orders-2",
        "proposed_paths": (PurePosixPath("/app"),),
        "status": RegistryProposalStatus.PENDING,
        "created_at": NOW,
    }
    fields.update(kwargs)
    return RegistryUpdateProposal(**fields)


def make_checkpoint(*, sequence: int = 1, **kwargs: object) -> Checkpoint:
    fields: dict[str, object] = {
        "checkpoint_id": f"cp-{sequence}",
        "agent_run_id": "run-1",
        "sequence": sequence,
        "status": AgentRunStatus.RUNNING,
        "round_number": 0,
        "usage": UsageCounters(rounds=sequence),
        "created_at": NOW,
    }
    fields.update(kwargs)
    return Checkpoint(**fields)


def make_boundary(*, through_sequence: int = 10, **kwargs: object) -> CompactBoundary:
    fields: dict[str, object] = {
        "agent_run_id": "run-1",
        "through_sequence": through_sequence,
        "memory_revision": 1,
        "summary": "compacted transcript",
        "created_at": NOW,
    }
    fields.update(kwargs)
    return CompactBoundary(**fields)


def make_receipt(
    *, child_run_id: str = "child-1", evidence_id: str = "ev-1", created_at: datetime = NOW
) -> ChildReportReceipt:
    report = ChildReport(
        agent_run_id=child_run_id,
        parent_run_id="run-1",
        status=ChildReportStatus.COMPLETE,
        summary="child found the cause",
        findings=("pool exhausted",),
        stop_reason=StopReason.COMPLETED,
        created_at=created_at,
    )
    return ChildReportReceipt(
        child_run_id=child_run_id,
        parent_run_id="run-1",
        report=report,
        evidence_id=evidence_id,
        created_at=created_at,
    )


def make_notification(sequence: int = 1) -> TranscriptMessage:
    return TranscriptMessage(
        agent_run_id="run-1", sequence=sequence, role=MessageRole.ASSISTANT,
        blocks=(TextBlock(text="child report received"),), created_at=NOW,
    )




def test_migrate_is_idempotent_and_creates_all_tables(tmp_path) -> None:
    store = make_store(tmp_path)
    store.migrate()  # second run must be a no-op
    with sqlite3.connect(tmp_path / "investigations.db") as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    expected = {
        "investigations",
        "agent_runs",
        "agent_rounds",
        "agent_checkpoints",
        "agent_session_memories",
        "tool_calls",
        "hypotheses",
        "conclusions",
        "delegated_tasks",
        "registry_update_proposals",
        "agent_transcript_messages",
        "agent_compact_boundaries",
        "agent_todos",
        "agent_compaction_state",
    }
    assert expected <= tables


def test_recovery_indexes_exist(tmp_path) -> None:
    make_store(tmp_path)
    with sqlite3.connect(tmp_path / "investigations.db") as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    assert {
        "idx_investigations_project_status",
        "idx_investigations_status",
        "idx_agent_runs_parent_status",
        "idx_tool_calls_status",
        "idx_proposals_status",
    } <= indexes


# -- investigations -----------------------------------------------------------


def test_create_and_get_investigation_roundtrip(tmp_path) -> None:
    store = make_store(tmp_path)
    investigation = make_investigation(
        budget=InvestigationBudget(max_children=2),
        usage=UsageCounters(rounds=3, children=1),
    )
    store.create_investigation(investigation)

    loaded = store.get_investigation("inv-1")
    assert loaded == investigation
    assert loaded.budget.max_children == 2
    assert loaded.usage.rounds == 3


def test_create_investigation_rejects_duplicate(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_investigation(make_investigation())
    with pytest.raises(AlreadyExists):
        store.create_investigation(make_investigation())


def test_get_missing_investigation_raises(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(InvestigationNotFound):
        store.get_investigation("nope")


def test_list_investigations_filters(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_investigation(
        make_investigation(investigation_id="inv-1", project_id="proj-1")
    )
    store.create_investigation(
        make_investigation(
            investigation_id="inv-2",
            project_id="proj-2",
            status=InvestigationStatus.COMPLETED,
        )
    )

    assert {i.investigation_id for i in store.list_investigations()} == {
        "inv-1",
        "inv-2",
    }
    assert {i.investigation_id for i in store.list_investigations(project_id="proj-1")} == {
        "inv-1"
    }
    assert {
        i.investigation_id
        for i in store.list_investigations(status=InvestigationStatus.COMPLETED)
    } == {"inv-2"}
    assert {
        i.investigation_id for i in store.list_investigations(incident_id="inc-123")
    } == {"inv-1", "inv-2"}


def test_list_non_terminal_investigations(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_investigation(make_investigation(investigation_id="inv-1"))
    store.create_investigation(
        make_investigation(
            investigation_id="inv-2", status=InvestigationStatus.COMPLETED
        )
    )
    store.create_investigation(
        make_investigation(
            investigation_id="inv-3", status=InvestigationStatus.PAUSED_BUDGET
        )
    )

    terminal_values = {s.value for s in INVESTIGATION_TERMINAL}
    non_terminal = store.list_non_terminal_investigations()
    assert {i.investigation_id for i in non_terminal} == {"inv-1", "inv-3"}
    assert all(i.status.value not in terminal_values for i in non_terminal)


def test_transition_investigation_status_sets_timestamps(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_investigation(
        make_investigation(status=InvestigationStatus.CREATED)
    )

    running = store.transition_investigation_status(
        "inv-1", InvestigationStatus.RUNNING, now=NOW
    )
    assert running.status is InvestigationStatus.RUNNING
    assert running.started_at == NOW

    completed = store.transition_investigation_status(
        "inv-1", InvestigationStatus.COMPLETED, now=NOW
    )
    assert completed.status is InvestigationStatus.COMPLETED
    assert completed.completed_at == NOW
    assert store.get_investigation("inv-1") == completed


def test_transition_investigation_illegal_raises(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_investigation(make_investigation())
    with pytest.raises(IllegalTransition):
        store.transition_investigation_status(
            "inv-1", InvestigationStatus.CANCELLED, now=NOW
        )  # RUNNING -> CANCELLED is illegal


def test_transition_investigation_cancel_request_is_atomic(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_investigation(make_investigation())

    cancelled = store.transition_investigation_status(
        "inv-1", InvestigationStatus.CANCEL_REQUESTED, now=NOW, stop_reason=StopReason.CANCELLED
    )
    assert cancelled.status is InvestigationStatus.CANCEL_REQUESTED
    assert cancelled.stop_reason is StopReason.CANCELLED

    # The conditional update must refuse the same transition from the new status.
    with pytest.raises(IllegalTransition):
        store.transition_investigation_status(
            "inv-1", InvestigationStatus.CANCEL_REQUESTED, now=NOW
        )


def test_update_investigation_preserves_status_guard(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_investigation(make_investigation(status=InvestigationStatus.RUNNING))

    updated = store.update_investigation(
        make_investigation(
            status=InvestigationStatus.RUNNING,
            usage=UsageCounters(rounds=4),
            updated_at=NOW,
        )
    )
    assert updated.usage.rounds == 4

    # A stale model whose status no longer matches must be rejected.
    store.transition_investigation_status("inv-1", InvestigationStatus.COMPLETED, now=NOW)
    with pytest.raises(ConcurrentModification):
        store.update_investigation(
            make_investigation(status=InvestigationStatus.RUNNING, updated_at=NOW)
        )


# -- agent runs ---------------------------------------------------------------


def test_create_and_get_agent_run_roundtrip(tmp_path) -> None:
    store = make_store(tmp_path)
    run = make_run(usage=UsageCounters(tool_calls=2))
    store.create_agent_run(run)
    assert store.get_agent_run("run-1") == run


def test_create_child_run_roundtrip(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_agent_run(make_run())
    child = make_container_child(
        scope=AgentScope(
            project_id="proj-1",
            target_id="prod-a",
            scope=LogScope.CONTAINER,
            service_name="orders",
            container_name="orders-1",
            allowed_container_paths=(PurePosixPath("/app"), PurePosixPath("/opt/orders")),
        )
    )
    store.create_agent_run(child)
    loaded = store.get_agent_run("child-1")
    assert loaded == child
    assert loaded.scope.allowed_container_paths == (
        PurePosixPath("/app"),
        PurePosixPath("/opt/orders"),
    )


def test_list_agent_runs_filters(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_agent_run(make_run(agent_run_id="run-1"))
    child = make_container_child(agent_run_id="child-1")
    store.create_agent_run(child)

    assert {r.agent_run_id for r in store.list_agent_runs(investigation_id="inv-1")} == {
        "run-1",
        "child-1",
    }
    assert {
        r.agent_run_id for r in store.list_agent_runs(parent_run_id="run-1")
    } == {"child-1"}
    assert {
        r.agent_run_id
        for r in store.list_agent_runs(status=AgentRunStatus.RUNNING)
    } == {"run-1", "child-1"}


def test_list_unfinished_children(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_agent_run(make_run(agent_run_id="run-1"))
    store.create_agent_run(make_container_child(agent_run_id="child-1"))
    store.create_agent_run(
        make_container_child(
            agent_run_id="child-2", status=AgentRunStatus.COMPLETED
        )
    )

    unfinished = store.list_unfinished_children()
    assert {r.agent_run_id for r in unfinished} == {"child-1"}

    by_inv = store.list_unfinished_children(investigation_id="inv-1")
    assert {r.agent_run_id for r in by_inv} == {"child-1"}


def test_transition_agent_run_status(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_agent_run(
        make_run(status=AgentRunStatus.CREATED, agent_run_id="run-1")
    )
    running = store.transition_agent_run_status(
        "run-1", AgentRunStatus.RUNNING, now=NOW
    )
    assert running.started_at == NOW
    completed = store.transition_agent_run_status(
        "run-1", AgentRunStatus.COMPLETED, now=NOW
    )
    assert completed.completed_at == NOW

    with pytest.raises(IllegalTransition):
        store.transition_agent_run_status("run-1", AgentRunStatus.RUNNING, now=NOW)


def test_waiting_approval_startup_queries(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_agent_run(
        make_run(agent_run_id="run-1", status=AgentRunStatus.WAITING_APPROVAL)
    )
    store.create_agent_run(
        make_run(agent_run_id="run-2", status=AgentRunStatus.RUNNING)
    )
    store.create_tool_call(
        make_tool_call(tool_call_id="tool-1", status=ToolCallStatus.WAITING_APPROVAL)
    )
    store.create_tool_call(
        make_tool_call(tool_call_id="tool-2", status=ToolCallStatus.RUNNING)
    )

    assert {r.agent_run_id for r in store.list_waiting_approval_runs()} == {"run-1"}
    assert {
        t.tool_call_id for t in store.list_waiting_approval_tool_calls()
    } == {"tool-1"}


# -- agent rounds -------------------------------------------------------------


def test_append_round_and_list(tmp_path) -> None:
    store = make_store(tmp_path)
    round_summary = AgentRound(
        agent_run_id="run-1",
        round_number=1,
        status=AgentRunStatus.WAITING_TOOL,
        provider_usage=ProviderUsage(input_tokens=10, output_tokens=20, output_bytes=30),
        usage=UsageCounters(rounds=1),
        stop_reason=None,
        created_at=NOW,
    )
    store.append_round(round_summary)

    assert store.list_rounds("run-1") == (round_summary,)


def test_append_round_rejects_duplicate_number(tmp_path) -> None:
    store = make_store(tmp_path)
    store.append_round(
        AgentRound(
            agent_run_id="run-1",
            round_number=1,
            status=AgentRunStatus.RUNNING,
            provider_usage=ProviderUsage(),
            usage=UsageCounters(),
            created_at=NOW,
        )
    )
    with pytest.raises(RoundConflict):
        store.append_round(
            AgentRound(
                agent_run_id="run-1",
                round_number=1,
                status=AgentRunStatus.RUNNING,
                provider_usage=ProviderUsage(),
                usage=UsageCounters(),
                created_at=NOW,
            )
        )


# -- checkpoints --------------------------------------------------------------


def test_checkpoint_append_list_and_latest(tmp_path) -> None:
    store = make_store(tmp_path)
    first = make_checkpoint(sequence=1)
    second = make_checkpoint(sequence=2, usage=UsageCounters(rounds=2))
    store.append_checkpoint(first)
    store.append_checkpoint(second)

    assert store.list_checkpoints("run-1") == (first, second)
    assert store.get_latest_checkpoint("run-1") == second


def test_checkpoint_append_only_rejects_duplicate_sequence(tmp_path) -> None:
    store = make_store(tmp_path)
    store.append_checkpoint(make_checkpoint(sequence=1))
    with pytest.raises(CheckpointConflict):
        store.append_checkpoint(make_checkpoint(sequence=1))


def test_get_latest_checkpoint_none_when_empty(tmp_path) -> None:
    store = make_store(tmp_path)
    assert store.get_latest_checkpoint("run-1") is None


def test_session_memory_is_append_only_and_latest_is_queryable(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_investigation(make_investigation())
    store.create_agent_run(make_run())
    first = SessionMemory(
        memory_id="mem-run-1-1",
        agent_run_id="run-1",
        investigation_id="inv-1",
        revision=1,
        through_round=4,
        objective="find checkout failures",
        evidence_ids=("ev-1",),
        created_at=NOW,
    )
    second = first.model_copy(
        update={
            "memory_id": "mem-run-1-2",
            "revision": 2,
            "through_round": 8,
            "evidence_ids": ("ev-1", "ev-2"),
        }
    )

    store.append_session_memory(first)
    store.append_session_memory(second)

    assert store.list_session_memories("run-1") == (first, second)
    assert store.get_latest_session_memory("run-1") == second


def test_session_memory_new_fields_have_backward_compatible_defaults(tmp_path) -> None:
    """Rows written by the pre-transcript prototype stay readable (T1)."""
    store = make_store(tmp_path)
    memory = SessionMemory(
        memory_id="mem-run-1-1",
        agent_run_id="run-1",
        investigation_id="inv-1",
        revision=1,
        through_round=4,
        objective="find checkout failures",
        created_at=NOW,
    )
    store.append_session_memory(memory)
    loaded = store.get_latest_session_memory("run-1")
    assert loaded is not None
    assert loaded.through_transcript_sequence == 0
    assert loaded.user_constraints == ()
    assert loaded.todos == ()
    assert loaded.next_actions == ()


# -- transcript messages, compact boundaries, work plan, compaction state -----


def test_compact_boundary_append_and_latest(tmp_path) -> None:
    store = make_store(tmp_path)
    first = make_boundary(through_sequence=10)
    second = make_boundary(
        through_sequence=20,
        memory_revision=2,
        summary="compacted through message 20",
    )
    store.append_compact_boundary(first)
    store.append_compact_boundary(second)
    assert store.get_latest_compact_boundary("run-1") == second


def test_compact_boundary_rejects_duplicate_through_sequence(tmp_path) -> None:
    store = make_store(tmp_path)
    store.append_compact_boundary(make_boundary(through_sequence=10))
    with pytest.raises(CompactBoundaryConflict):
        store.append_compact_boundary(make_boundary(through_sequence=10))


def test_get_latest_compact_boundary_none_when_empty(tmp_path) -> None:
    store = make_store(tmp_path)
    assert store.get_latest_compact_boundary("run-1") is None


def test_replace_todos_roundtrip_and_clears_prior_plan(tmp_path) -> None:
    store = make_store(tmp_path)
    first = (
        TodoItem(
            todo_id="one",
            content="inspect logs",
            status=TodoStatus.PENDING,
            updated_at=NOW,
        ),
        TodoItem(
            todo_id="two",
            content="check database",
            status=TodoStatus.IN_PROGRESS,
            updated_at=NOW,
        ),
    )
    store.replace_todos("run-1", first)
    assert store.list_todos("run-1") == first

    second = (
        TodoItem(
            todo_id="two",
            content="check database",
            status=TodoStatus.COMPLETED,
            updated_at=NOW,
        ),
        TodoItem(
            todo_id="three",
            content="verify root cause",
            status=TodoStatus.PENDING,
            updated_at=NOW,
        ),
    )
    store.replace_todos("run-1", second)
    assert store.list_todos("run-1") == second


def test_replace_todos_empty_clears_plan(tmp_path) -> None:
    store = make_store(tmp_path)
    store.replace_todos(
        "run-1",
        (
            TodoItem(
                todo_id="one",
                content="inspect logs",
                status=TodoStatus.PENDING,
                updated_at=NOW,
            ),
        ),
    )
    store.replace_todos("run-1", ())
    assert store.list_todos("run-1") == ()


def test_replace_todos_rejects_duplicate_todo_id(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="duplicate"):
        store.replace_todos(
            "run-1",
            (
                TodoItem(
                    todo_id="one",
                    content="inspect logs",
                    status=TodoStatus.PENDING,
                    updated_at=NOW,
                ),
                TodoItem(
                    todo_id="one",
                    content="check database",
                    status=TodoStatus.PENDING,
                    updated_at=NOW,
                ),
            ),
        )


def test_compaction_state_put_and_get(tmp_path) -> None:
    store = make_store(tmp_path)
    state = CompactionState(
        agent_run_id="run-1",
        consecutive_failures=2,
        reactive_round=5,
        latest_boundary_sequence=20,
        updated_at=NOW,
    )
    store.put_compaction_state(state)
    assert store.get_compaction_state("run-1") == state


def test_put_compaction_state_overwrites(tmp_path) -> None:
    store = make_store(tmp_path)
    store.put_compaction_state(
        CompactionState(
            agent_run_id="run-1", consecutive_failures=1, updated_at=NOW
        )
    )
    store.put_compaction_state(
        CompactionState(
            agent_run_id="run-1", consecutive_failures=0, updated_at=NOW
        )
    )
    assert store.get_compaction_state("run-1").consecutive_failures == 0


def test_get_compaction_state_none_when_empty(tmp_path) -> None:
    store = make_store(tmp_path)
    assert store.get_compaction_state("run-1") is None


# -- tool calls ---------------------------------------------------------------


def test_create_and_get_tool_call_roundtrip(tmp_path) -> None:
    store = make_store(tmp_path)
    tool_call = make_tool_call()
    store.create_tool_call(tool_call)
    assert store.get_tool_call("tool-1") == tool_call


def test_get_missing_tool_call_raises(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ToolCallNotFound):
        store.get_tool_call("nope")


def test_transition_tool_call_status(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_tool_call(make_tool_call())

    running = store.transition_tool_call_status(
        "tool-1", ToolCallStatus.RUNNING, now=NOW
    )
    assert running.started_at == NOW

    succeeded = store.transition_tool_call_status(
        "tool-1",
        ToolCallStatus.SUCCEEDED,
        now=NOW,
        output_bytes=512,
        evidence_ids=("ev-1",),
    )
    assert succeeded.finished_at == NOW
    assert succeeded.output_bytes == 512
    assert succeeded.evidence_ids == ("ev-1",)

    with pytest.raises(IllegalTransition):
        store.transition_tool_call_status("tool-1", ToolCallStatus.RUNNING, now=NOW)


def test_list_in_flight_tool_calls(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_tool_call(make_tool_call(tool_call_id="tool-1", status=ToolCallStatus.PLANNED))
    store.create_tool_call(
        make_tool_call(tool_call_id="tool-2", status=ToolCallStatus.RUNNING)
    )
    store.create_tool_call(
        make_tool_call(tool_call_id="tool-3", status=ToolCallStatus.SUCCEEDED)
    )

    in_flight = store.list_in_flight_tool_calls()
    assert {t.tool_call_id for t in in_flight} == {"tool-1", "tool-2"}


# -- hypotheses ---------------------------------------------------------------


def test_create_hypothesis_derives_investigation(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_agent_run(make_run())
    hypothesis = make_hypothesis()
    store.create_hypothesis(hypothesis)

    assert store.get_hypothesis("hyp-1") == hypothesis
    with sqlite3.connect(tmp_path / "investigations.db") as conn:
        row = conn.execute(
            "SELECT investigation_id FROM hypotheses WHERE hypothesis_id = ?",
            ("hyp-1",),
        ).fetchone()
    assert row[0] == "inv-1"


def test_create_hypothesis_requires_existing_run(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(AgentRunNotFound):
        store.create_hypothesis(make_hypothesis())


def test_list_hypotheses_filters(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_agent_run(make_run())
    store.create_hypothesis(make_hypothesis(hypothesis_id="hyp-1"))
    store.create_hypothesis(
        make_hypothesis(
            hypothesis_id="hyp-2",
            status=HypothesisStatus.ACTIVE,
        )
    )
    assert {h.hypothesis_id for h in store.list_hypotheses()} == {"hyp-1", "hyp-2"}
    assert {
        h.hypothesis_id for h in store.list_hypotheses(agent_run_id="run-1")
    } == {"hyp-1", "hyp-2"}


def test_transition_hypothesis_status(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_agent_run(make_run())
    store.create_hypothesis(make_hypothesis())

    active = store.transition_hypothesis_status(
        "hyp-1", HypothesisStatus.ACTIVE, now=NOW
    )
    assert active.status is HypothesisStatus.ACTIVE


# -- conclusions --------------------------------------------------------------


def test_create_and_list_conclusions(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_agent_run(make_run())
    conclusion = Conclusion(
        summary="database pool is exhausted",
        facts=("orders container reports pool timeout",),
        evidence_ids=("ev-1",),
    )
    store.create_conclusion("run-1", "inv-1", conclusion, now=NOW)

    assert store.list_conclusions(agent_run_id="run-1") == (conclusion,)
    assert store.list_conclusions(investigation_id="inv-1") == (conclusion,)
    assert store.list_conclusions(agent_run_id="other-run") == ()


def test_create_conclusion_rejects_cross_investigation_attribution(tmp_path) -> None:
    """A conclusion's agent run must belong to the named investigation (M3)."""
    store = make_store(tmp_path)
    store.create_agent_run(make_run())
    conclusion = Conclusion(summary="database pool is exhausted")
    with pytest.raises(IllegalTransition):
        store.create_conclusion("run-1", "inv-OTHER", conclusion, now=NOW)


# -- delegated tasks ----------------------------------------------------------


def test_create_get_and_list_delegated_tasks(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_agent_run(make_run())
    child = make_container_child()
    store.create_agent_run(child)
    package = DelegatedTaskPackage(
        child_run_id="child-1",
        parent_run_id="run-1",
        investigation_id="inv-1",
        task_prompt="inspect the orders container",
        scope=child.scope,
        budget=AgentBudget(),
        evidence_ids=(),
    )
    store.create_delegated_task(package, now=NOW)

    assert store.get_delegated_task("child-1") == package
    assert store.list_delegated_tasks(parent_run_id="run-1") == (package,)
    assert store.list_delegated_tasks(investigation_id="inv-1") == (package,)


def test_get_missing_delegated_task_raises(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(DelegatedTaskNotFound):
        store.get_delegated_task("nope")


# -- registry update proposals ------------------------------------------------


def test_create_and_get_proposal_roundtrip(tmp_path) -> None:
    store = make_store(tmp_path)
    proposal = make_proposal()
    store.create_proposal(proposal)
    assert store.get_proposal("prop-1") == proposal


def test_list_pending_proposals_and_transition(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_proposal(make_proposal(proposal_id="prop-1"))
    store.create_proposal(
        make_proposal(
            proposal_id="prop-2", status=RegistryProposalStatus.APPROVED
        )
    )

    pending = store.list_pending_proposals()
    assert {p.proposal_id for p in pending} == {"prop-1"}

    decided = store.transition_proposal_status(
        "prop-1", RegistryProposalStatus.APPROVED, now=NOW
    )
    assert decided.status is RegistryProposalStatus.APPROVED
    assert decided.decided_at == NOW
    assert store.list_pending_proposals() == ()


def test_transition_non_pending_proposal_raises(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_proposal(
        make_proposal(status=RegistryProposalStatus.APPROVED)
    )
    with pytest.raises(IllegalTransition):
        store.transition_proposal_status("prop-1", RegistryProposalStatus.REJECTED, now=NOW)


def test_get_missing_proposal_raises(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ProposalNotFound):
        store.get_proposal("nope")


# -- contract JSON hygiene ----------------------------------------------------


def test_record_json_only_serializes_validated_contracts(tmp_path) -> None:
    """Stored JSON must round-trip through the immutable Pydantic contracts."""
    store = make_store(tmp_path)
    investigation = make_investigation()
    store.create_investigation(investigation)
    store.create_agent_run(make_run())
    tool_call = make_tool_call()
    store.create_tool_call(tool_call)
    store.create_hypothesis(make_hypothesis())

    with sqlite3.connect(tmp_path / "investigations.db") as conn:
        inv_json = conn.execute(
            "SELECT record_json FROM investigations WHERE investigation_id = ?",
            ("inv-1",),
        ).fetchone()[0]
        run_json = conn.execute(
            "SELECT record_json FROM agent_runs WHERE agent_run_id = ?",
            ("run-1",),
        ).fetchone()[0]
        tool_json = conn.execute(
            "SELECT record_json FROM tool_calls WHERE tool_call_id = ?",
            ("tool-1",),
        ).fetchone()[0]

    assert Investigation.model_validate_json(inv_json) == investigation
    assert AgentRun.model_validate_json(run_json) == make_run()
    assert ToolCall.model_validate_json(tool_json) == tool_call


def test_get_evidence_reads_shared_evidence_store(tmp_path) -> None:
    """InvestigationStore.get_evidence reloads the complete tool output (T3)."""
    from incidentlens_control_plane.evidence.store import EvidenceStore
    from incidentlens_control_plane.evidence.types import EvidenceKind, EvidenceRef

    store = make_store(tmp_path)
    evidence_store = EvidenceStore(store.connection_factory)
    evidence_store.migrate()
    ref = evidence_store.create(
        EvidenceRef(
            evidence_ref_id="ev-tool-1",
            incident_id="inc-123",
            evidence_kind=EvidenceKind.COMMAND_OUTPUT,
            agent_run_id="run-1",
            project_id="proj-1",
            target_id="prod-a",
            service_name="orders",
            content_redacted="x" * 250_000,
            content_sha256=hashlib.sha256(b"x" * 250_000).hexdigest(),
            redaction_summary={},
            created_at=NOW,
            created_by="agent",
        )
    )
    assert store.get_evidence("ev-tool-1").content_redacted == ref.content_redacted


def test_raw_transcript_never_enters_round_json(tmp_path) -> None:
    """The round summary contract carries only structured audit fields."""
    store = make_store(tmp_path)
    store.append_round(
        AgentRound(
            agent_run_id="run-1",
            round_number=1,
            status=AgentRunStatus.WAITING_TOOL,
            provider_usage=ProviderUsage(output_bytes=42),
            usage=UsageCounters(),
            created_at=NOW,
        )
    )
    with sqlite3.connect(tmp_path / "investigations.db") as conn:
        raw = conn.execute(
            "SELECT record_json FROM agent_rounds WHERE agent_run_id = ?",
            ("run-1",),
        ).fetchone()[0]
    assert "transcript" not in raw
    assert "reasoning" not in raw


# -- C1: derived models are re-validated before persisting ---------------------


def test_transition_tool_call_rejects_duplicate_evidence_ids(tmp_path) -> None:
    """Duplicate evidence ids must not pollute the DB (C1)."""
    store = make_store(tmp_path)
    store.create_tool_call(make_tool_call())

    with pytest.raises(ValidationError):
        store.transition_tool_call_status(
            "tool-1", ToolCallStatus.SUCCEEDED, now=NOW, evidence_ids=("ev-1", "ev-1")
        )

    # The stored row is unchanged: still planned, no evidence attached.
    assert store.get_tool_call("tool-1").status is ToolCallStatus.PLANNED


def test_transition_tool_call_rejects_empty_evidence_ids(tmp_path) -> None:
    """Empty evidence id strings must not pollute the DB (C1)."""
    store = make_store(tmp_path)
    store.create_tool_call(make_tool_call())

    with pytest.raises(ValidationError):
        store.transition_tool_call_status(
            "tool-1", ToolCallStatus.SUCCEEDED, now=NOW, evidence_ids=("ev-1", " ")
        )

    assert store.get_tool_call("tool-1").status is ToolCallStatus.PLANNED


def test_transition_tool_call_rejects_negative_output_bytes(tmp_path) -> None:
    """Negative output bytes must not pollute the DB (C1)."""
    store = make_store(tmp_path)
    store.create_tool_call(make_tool_call())

    with pytest.raises(ValidationError):
        store.transition_tool_call_status(
            "tool-1", ToolCallStatus.SUCCEEDED, now=NOW, output_bytes=-1
        )

    assert store.get_tool_call("tool-1").status is ToolCallStatus.PLANNED


def test_transition_tool_call_rejects_overlong_error(tmp_path) -> None:
    """An over-long redacted error summary must not pollute the DB (C1)."""
    store = make_store(tmp_path)
    store.create_tool_call(make_tool_call())

    with pytest.raises(ValidationError):
        store.transition_tool_call_status(
            "tool-1", ToolCallStatus.SUCCEEDED, now=NOW, error_redacted="x" * 2001
        )

    assert store.get_tool_call("tool-1").status is ToolCallStatus.PLANNED


def test_transition_investigation_rejects_bogus_stop_reason(tmp_path) -> None:
    """An invalid stop reason must not pollute the DB (C1)."""
    store = make_store(tmp_path)
    store.create_investigation(make_investigation(status=InvestigationStatus.RUNNING))

    with pytest.raises(ValidationError):
        store.transition_investigation_status(
            "inv-1",
            InvestigationStatus.PAUSED_BUDGET,
            now=NOW,
            stop_reason="bogus",  # type: ignore[arg-type]
        )

    assert store.get_investigation("inv-1").status is InvestigationStatus.RUNNING


def test_transition_agent_run_rejects_bogus_stop_reason(tmp_path) -> None:
    """An invalid stop reason must not pollute the DB (C1)."""
    store = make_store(tmp_path)
    store.create_agent_run(make_run(status=AgentRunStatus.RUNNING))

    with pytest.raises(ValidationError):
        store.transition_agent_run_status(
            "run-1",
            AgentRunStatus.PAUSED_BUDGET,
            now=NOW,
            stop_reason="bogus",  # type: ignore[arg-type]
        )

    assert store.get_agent_run("run-1").status is AgentRunStatus.RUNNING


# -- I1: hypothesis state machine is enforced in the store --------------------


def test_transition_hypothesis_rejects_rollback(tmp_path) -> None:
    """A confirmed hypothesis cannot roll back to ACTIVE (I1)."""
    store = make_store(tmp_path)
    store.create_agent_run(make_run())
    store.create_hypothesis(make_hypothesis())

    confirmed = store.transition_hypothesis_status(
        "hyp-1", HypothesisStatus.CONFIRMED, now=NOW
    )
    assert confirmed.status is HypothesisStatus.CONFIRMED

    with pytest.raises(IllegalTransition):
        store.transition_hypothesis_status("hyp-1", HypothesisStatus.ACTIVE, now=NOW)

    assert store.get_hypothesis("hyp-1").status is HypothesisStatus.CONFIRMED


def test_transition_hypothesis_confirmed_is_terminal(tmp_path) -> None:
    """A confirmed hypothesis is absorbing: it cannot be superseded or rolled back (I1)."""
    store = make_store(tmp_path)
    store.create_agent_run(make_run())
    store.create_hypothesis(make_hypothesis())

    confirmed = store.transition_hypothesis_status(
        "hyp-1", HypothesisStatus.CONFIRMED, now=NOW
    )
    assert confirmed.status is HypothesisStatus.CONFIRMED

    with pytest.raises(IllegalTransition):
        store.transition_hypothesis_status("hyp-1", HypothesisStatus.SUPERSEDED, now=NOW)
    with pytest.raises(IllegalTransition):
        store.transition_hypothesis_status("hyp-1", HypothesisStatus.ACTIVE, now=NOW)

    assert store.get_hypothesis("hyp-1").status is HypothesisStatus.CONFIRMED


def test_hypothesis_state_machine_shared_with_types(tmp_path) -> None:
    """types.HypothesisStatus is the same enum the state machine guards (I1)."""
    from incidentlens_control_plane.investigation import types as investigation_types

    assert investigation_types.HypothesisStatus is HypothesisStatus
    assert HYPOTHESIS_STATE_MACHINE.can_transition(
        HypothesisStatus.CONFIRMED, HypothesisStatus.ACTIVE
    ) is False


# -- M2: proposal decisions are single-use and cannot self-transition ---------


def test_proposal_rejects_self_transition(tmp_path) -> None:
    """A pending proposal cannot be re-decided onto itself (M2)."""
    store = make_store(tmp_path)
    store.create_proposal(make_proposal())

    with pytest.raises(IllegalTransition):
        store.transition_proposal_status(
            "prop-1", RegistryProposalStatus.PENDING, now=NOW
        )
    assert store.get_proposal("prop-1").status is RegistryProposalStatus.PENDING


def test_proposal_rejects_second_decision(tmp_path) -> None:
    """A decided proposal cannot be re-decided (M2)."""
    store = make_store(tmp_path)
    store.create_proposal(make_proposal())
    store.transition_proposal_status("prop-1", RegistryProposalStatus.APPROVED, now=NOW)

    with pytest.raises(IllegalTransition):
        store.transition_proposal_status("prop-1", RegistryProposalStatus.REJECTED, now=NOW)
    assert store.get_proposal("prop-1").status is RegistryProposalStatus.APPROVED


# -- M3: cross-investigation attribution is rejected --------------------------


def test_create_child_run_rejects_cross_investigation_parent(tmp_path) -> None:
    """A child run must reference a parent in the same investigation (M3)."""
    store = make_store(tmp_path)
    store.create_agent_run(make_run())

    with pytest.raises(IllegalTransition):
        store.create_agent_run(
            make_container_child(
                agent_run_id="child-1",
                parent_run_id="run-1",
                investigation_id="inv-OTHER",
            )
        )


def test_create_child_run_requires_existing_parent(tmp_path) -> None:
    """A child run must reference an existing parent (M3)."""
    store = make_store(tmp_path)
    with pytest.raises(AgentRunNotFound):
        store.create_agent_run(
            make_container_child(agent_run_id="child-1", parent_run_id="missing")
        )


def test_create_delegated_task_rejects_cross_investigation_parent(tmp_path) -> None:
    """A delegated task's parent must be in the same investigation (M3)."""
    store = make_store(tmp_path)
    store.create_agent_run(make_run())
    child = make_container_child()
    store.create_agent_run(child)
    package = DelegatedTaskPackage(
        child_run_id="child-1",
        parent_run_id="run-1",
        investigation_id="inv-OTHER",
        task_prompt="inspect the orders container",
        scope=child.scope,
        budget=AgentBudget(),
        evidence_ids=(),
    )
    with pytest.raises(IllegalTransition):
        store.create_delegated_task(package, now=NOW)


def test_child_report_receipt_is_append_once(tmp_path) -> None:
    store = make_store(tmp_path)
    receipt = make_receipt()
    assert store.put_child_report_receipt(receipt) == receipt
    assert store.put_child_report_receipt(receipt) == receipt
    assert store.list_undelivered_child_report_receipts("run-1") == (receipt,)


def test_conflicting_receipt_for_same_child_is_rejected(tmp_path) -> None:
    store = make_store(tmp_path)
    store.put_child_report_receipt(make_receipt())
    with pytest.raises(ChildReportReceiptConflict):
        store.put_child_report_receipt(make_receipt(evidence_id="ev-other"))


def test_receipt_delivery_is_idempotent(tmp_path) -> None:
    store = make_store(tmp_path)
    investigation = make_investigation()
    parent = make_run()
    store.create_investigation(investigation)
    store.create_agent_run(parent)
    receipt = make_receipt()
    store.put_child_report_receipt(receipt)
    delivered = store.deliver_child_report_receipt(
        receipt.child_run_id,
        parent=parent,
        investigation=investigation,
        notification=make_notification(),
        delivered_at=NOW,
    )
    assert delivered.delivered_at == NOW
    assert store.deliver_child_report_receipt(
        receipt.child_run_id,
        parent=parent,
        investigation=investigation,
        notification=make_notification(2),
        delivered_at=datetime(2026, 8, 13, tzinfo=UTC),
    ) == delivered
    assert store.list_transcript_messages("run-1") == (make_notification(),)


def test_receipt_delivery_rejects_fabricated_cross_investigation_parent(tmp_path) -> None:
    store = make_store(tmp_path)
    investigation = make_investigation()
    parent = make_run()
    store.create_investigation(investigation)
    store.create_agent_run(parent)
    receipt = make_receipt()
    store.put_child_report_receipt(receipt)
    fabricated_parent = make_run(investigation_id="inv-OTHER")
    with pytest.raises(ValueError):
        store.deliver_child_report_receipt(
            receipt.child_run_id,
            parent=fabricated_parent,
            investigation=investigation,
            notification=make_notification(),
            delivered_at=NOW,
        )
    assert store.get_child_report_receipt(receipt.child_run_id).delivered_at is None
    assert store.list_transcript_messages("run-1") == ()
    assert store.get_agent_run("run-1") == parent
    assert store.get_investigation("inv-1") == investigation


def test_receipt_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        make_receipt(created_at=datetime(2026, 8, 13))


def test_receipt_delivery_rolls_back_on_transcript_conflict(tmp_path) -> None:
    store = make_store(tmp_path)
    investigation = make_investigation()
    parent = make_run()
    store.create_investigation(investigation)
    store.create_agent_run(parent)
    receipt = make_receipt()
    store.put_child_report_receipt(receipt)
    store.append_transcript_message(make_notification())
    changed_parent = make_run(
        usage=UsageCounters(rounds=1),
        updated_at=NOW,
    )
    changed_investigation = make_investigation(
        usage=UsageCounters(rounds=1),
        updated_at=NOW,
    )
    with pytest.raises(TranscriptConflict):
        store.deliver_child_report_receipt(
            receipt.child_run_id,
            parent=changed_parent,
            investigation=changed_investigation,
            notification=make_notification(),
            delivered_at=NOW,
        )
    assert store.get_child_report_receipt(receipt.child_run_id).delivered_at is None
    assert store.get_agent_run("run-1") == parent
    assert store.get_investigation("inv-1") == investigation
