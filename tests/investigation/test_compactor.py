"""Semantic and reactive compaction contract tests.

Covers the tool-free ``ContextCompactor`` contract, the schema validation the
manager applies to compactor output (evidence ownership, monotonic boundary,
work-state presence, redaction/length), the failure breaker persistence, the
atomic ``commit_compaction`` write, and reactive compaction that keeps the most
recent groups whole while refusing a second reactive attempt per round.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from incidentlens_control_plane.investigation.compactor import (
    CompactionCircuitOpen,
    CompactionRejected,
    CompactionRequest,
    ContextCompactor,
)
from incidentlens_control_plane.investigation.context import (
    ActiveContext,
    AgentContextManager,
    ContextBudgetPolicy,
)
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.store import (
    CompactBoundaryConflict,
    InvestigationStore,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    CompactBoundary,
    CompactionState,
    EvidenceReference,
    Investigation,
    InvestigationBudget,
    MessageRole,
    SessionMemory,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope

NOW = datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path) -> InvestigationStore:
    """An InvestigationStore over one SQLite file (no evidence store needed)."""
    factory = lambda: sqlite3.connect(tmp_path / "compactor.db")  # noqa: E731
    store = InvestigationStore(factory)
    store.migrate()
    return store


def manager(
    store: InvestigationStore, *, compactor: ContextCompactor | None = None
) -> AgentContextManager:
    """Build a manager with a fixed clock and the optional injected compactor."""
    return AgentContextManager(store, compactor=compactor, now=lambda: NOW)


def memory_with(*, evidence_ids: tuple[str, ...] = (), **overrides: object) -> SessionMemory:
    """A valid compactor-produced memory for ``run-1``/``inv-1``."""
    fields: dict[str, object] = {
        "memory_id": "mem-run-1-1",
        "agent_run_id": "run-1",
        "investigation_id": "inv-1",
        "revision": 1,
        "through_round": 1,
        "through_transcript_sequence": 1,
        "objective": "find the root cause of checkout 502s",
        "evidence_ids": evidence_ids,
        "created_at": NOW,
    }
    fields.update(overrides)
    return SessionMemory(**fields)


def run_with(*evidence_ids: str, **overrides: object) -> AgentRun:
    """A parent ``AgentRun`` for ``run-1`` owning the given evidence ids."""
    fields: dict[str, object] = {
        "agent_run_id": "run-1",
        "investigation_id": "inv-1",
        "kind": AgentRunKind.PARENT,
        "scope": AgentScope(
            project_id="checkout", target_id="prod-a", scope=LogScope.HOST
        ),
        "status": AgentRunStatus.RUNNING,
        "budget": AgentBudget(),
        "usage": UsageCounters(rounds=1),
        "evidence": tuple(
            EvidenceReference(
                evidence_id=evidence_id,
                operation_id=f"op-{evidence_id}",
                summary=f"output of {evidence_id}",
            )
            for evidence_id in evidence_ids
        ),
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return AgentRun(**fields)


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


def seed_groups(store: InvestigationStore, *, count: int = 8) -> None:
    """Append ``count`` assistant tool-use/tool-result message pairs."""
    for index in range(count):
        sequence = index * 2 + 1
        store.append_transcript_message(
            TranscriptMessage(
                agent_run_id="run-1",
                sequence=sequence,
                role=MessageRole.ASSISTANT,
                blocks=(
                    ToolUseBlock(
                        tool_call_id=f"call-{index}",
                        tool_name="logs.query",
                        arguments={},
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
                        status=ToolCallStatus.SUCCEEDED,
                        content=f"result {index}",
                        evidence_ids=(),
                        persisted_output=False,
                    ),
                ),
                created_at=NOW,
            )
        )


def boundary_sequences(store: InvestigationStore, agent_run_id: str) -> set[int]:
    """Return the through-sequences of every compact boundary for a run."""
    with store.connection_factory() as conn:
        rows = conn.execute(
            "SELECT through_sequence FROM agent_compact_boundaries "
            "WHERE agent_run_id = ?",
            (agent_run_id,),
        ).fetchall()
    return {row[0] for row in rows}


class RecordingCompactor:
    """A compactor that records every request and returns a scripted memory.

    With ``adapt=True`` the returned memory's revision and
    ``through_transcript_sequence`` are aligned to the request, so repeated
    compactions succeed instead of colliding on a stale revision/boundary.
    """

    def __init__(self, memory: SessionMemory, *, adapt: bool = False) -> None:
        self._memory = memory
        self._adapt = adapt
        self.requests: list[CompactionRequest] = []

    async def compact(self, request: CompactionRequest) -> SessionMemory:
        self.requests.append(request)
        memory = self._memory
        if self._adapt:
            revision = (
                (request.prior_memory.revision + 1)
                if request.prior_memory is not None
                else 1
            )
            memory = memory.model_copy(
                update={
                    "revision": revision,
                    "memory_id": f"mem-{request.agent_run_id}-{revision}",
                    "through_transcript_sequence": max(
                        request.through_sequence, memory.through_transcript_sequence
                    ),
                }
            )
        return memory


class FailingCompactor:
    """A compactor that always rejects, recording how many times it was called."""

    def __init__(self) -> None:
        self.calls = 0

    async def compact(self, request: CompactionRequest) -> SessionMemory:
        self.calls += 1
        raise CompactionRejected("scripted compactor failure")


# -- brief Step 1 tests --------------------------------------------------------


@pytest.mark.asyncio
async def test_compactor_has_no_tools_and_preserves_owned_evidence(store) -> None:
    compactor = RecordingCompactor(memory_with(evidence_ids=("ev-1",)))
    memory = await manager(store, compactor=compactor).semantic_compact(
        run_with("ev-1")
    )
    assert "tool_schemas" not in type(compactor.requests[0]).model_fields
    assert memory.evidence_ids == ("ev-1",)


@pytest.mark.asyncio
async def test_foreign_evidence_rejects_memory_revision(store) -> None:
    compactor = RecordingCompactor(memory_with(evidence_ids=("foreign",)))
    with pytest.raises(CompactionRejected, match="foreign"):
        await manager(store, compactor=compactor).semantic_compact(run_with("ev-1"))


@pytest.mark.asyncio
async def test_three_failures_open_circuit(store) -> None:
    compactor = FailingCompactor()
    manager_ = manager(store, compactor=compactor)
    for _ in range(3):
        with pytest.raises(CompactionRejected):
            await manager_.semantic_compact(run_with("ev-1"))
    with pytest.raises(CompactionCircuitOpen):
        await manager_.semantic_compact(run_with("ev-1"))
    assert compactor.calls == 3


@pytest.mark.asyncio
async def test_configured_breaker_threshold_is_enforced(store) -> None:
    manager_ = AgentContextManager(
        store,
        compactor=FailingCompactor(),
        policy=ContextBudgetPolicy(compact_max_failures=1),
        now=lambda: NOW,
    )
    with pytest.raises(CompactionRejected):
        await manager_.semantic_compact(run_with("ev-1"))
    with pytest.raises(CompactionCircuitOpen):
        await manager_.semantic_compact(run_with("ev-1"))


# -- validation ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_foreign_evidence_increments_breaker(store) -> None:
    compactor = RecordingCompactor(memory_with(evidence_ids=("foreign",)))
    manager_ = manager(store, compactor=compactor)
    with pytest.raises(CompactionRejected, match="foreign"):
        await manager_.semantic_compact(run_with("ev-1"))
    state = store.get_compaction_state("run-1")
    assert state is not None
    assert state.consecutive_failures == 1


@pytest.mark.asyncio
async def test_non_monotonic_boundary_rejected(store) -> None:
    seed_groups(store, count=2)  # head covers sequences 1-4
    compactor = RecordingCompactor(memory_with(through_transcript_sequence=1))
    with pytest.raises(CompactionRejected, match="monotonic"):
        await manager(store, compactor=compactor).semantic_compact(run_with("ev-1"))


@pytest.mark.asyncio
async def test_unredacted_text_rejected(store) -> None:
    compactor = RecordingCompactor(memory_with(objective="password=supersecret"))
    with pytest.raises(CompactionRejected, match="redact"):
        await manager(store, compactor=compactor).semantic_compact(run_with("ev-1"))


@pytest.mark.asyncio
async def test_oversized_text_field_rejected(store) -> None:
    # SessionMemory does not bound tuple-element widths; the validator does.
    compactor = RecordingCompactor(memory_with(confirmed_facts=("x" * 401,)))
    with pytest.raises(CompactionRejected, match="exceeds"):
        await manager(store, compactor=compactor).semantic_compact(run_with("ev-1"))


@pytest.mark.asyncio
async def test_wrong_run_identity_rejected(store) -> None:
    compactor = RecordingCompactor(memory_with(agent_run_id="other-run"))
    with pytest.raises(CompactionRejected, match="other-run"):
        await manager(store, compactor=compactor).semantic_compact(run_with("ev-1"))


@pytest.mark.asyncio
async def test_wrong_investigation_rejected(store) -> None:
    compactor = RecordingCompactor(memory_with(investigation_id="inv-OTHER"))
    with pytest.raises(CompactionRejected, match="inv-OTHER"):
        await manager(store, compactor=compactor).semantic_compact(run_with("ev-1"))


@pytest.mark.asyncio
async def test_missing_compactor_is_rejected(store) -> None:
    with pytest.raises(CompactionRejected, match="no semantic compactor"):
        await manager(store).semantic_compact(run_with("ev-1"))


# -- breaker persistence -------------------------------------------------------


@pytest.mark.asyncio
async def test_success_persists_memory_boundary_and_resets_failures(store) -> None:
    compactor = RecordingCompactor(memory_with(evidence_ids=("ev-1",)), adapt=True)
    manager_ = manager(store, compactor=compactor)
    run = run_with("ev-1")

    # Two failures trip the counter, then one success resets it.
    failing = FailingCompactor()
    manager_ = manager(store, compactor=failing)
    for _ in range(2):
        with pytest.raises(CompactionRejected):
            await manager_.semantic_compact(run)
    assert store.get_compaction_state("run-1").consecutive_failures == 2

    manager_ = manager(store, compactor=compactor)
    memory = await manager_.semantic_compact(run)
    assert memory.revision == 1
    assert store.get_latest_session_memory("run-1") == memory
    boundary = store.get_latest_compact_boundary("run-1")
    assert boundary is not None
    assert boundary.through_sequence == memory.through_transcript_sequence
    assert boundary.memory_revision == memory.revision
    state = store.get_compaction_state("run-1")
    assert state is not None
    assert state.consecutive_failures == 0


# -- commit_compaction atomicity ------------------------------------------------


def test_commit_compaction_persists_all_three(store) -> None:
    memory = memory_with(evidence_ids=("ev-1",), through_transcript_sequence=5)
    boundary = CompactBoundary(
        agent_run_id="run-1",
        through_sequence=5,
        memory_revision=1,
        summary="semantic compact through transcript sequence 5",
        created_at=NOW,
    )
    state = CompactionState(
        agent_run_id="run-1",
        consecutive_failures=0,
        latest_boundary_sequence=5,
        updated_at=NOW,
    )
    store.commit_compaction(memory, boundary, state)
    assert store.get_latest_session_memory("run-1") == memory
    assert store.get_latest_compact_boundary("run-1") == boundary
    assert store.get_compaction_state("run-1") == state


def test_commit_compaction_conflict_rolls_back_all_three(store) -> None:
    store.append_compact_boundary(
        CompactBoundary(
            agent_run_id="run-1",
            through_sequence=10,
            memory_revision=1,
            summary="existing boundary",
            created_at=NOW,
        )
    )
    memory = memory_with(through_transcript_sequence=10)
    boundary = CompactBoundary(
        agent_run_id="run-1",
        through_sequence=10,
        memory_revision=1,
        summary="duplicate boundary",
        created_at=NOW,
    )
    state = CompactionState(
        agent_run_id="run-1",
        consecutive_failures=0,
        latest_boundary_sequence=10,
        updated_at=NOW,
    )
    with pytest.raises(CompactBoundaryConflict):
        store.commit_compaction(memory, boundary, state)
    # The memory insert and breaker upsert rolled back with the failed boundary.
    assert store.get_latest_session_memory("run-1") is None
    assert store.get_compaction_state("run-1") is None


# -- reactive compaction -------------------------------------------------------


@pytest.mark.asyncio
async def test_reactive_compact_keeps_recent_groups_and_records_round(store) -> None:
    store.create_investigation(investigation())
    seed_groups(store, count=8)  # 8 pairs: sequences 1-16
    compactor = RecordingCompactor(memory_with(evidence_ids=("ev-1",)), adapt=True)
    run = run_with("ev-1")
    active = await manager(store, compactor=compactor).reactive_compact(
        run, keep_recent_groups=5
    )
    assert isinstance(active, ActiveContext)
    assert active.memory is not None
    # head (first 3 pairs) is compacted; tail (last 5 pairs) is replayed whole.
    replay_ids = {
        block.tool_call_id
        for message in active.messages
        for block in message.blocks
        if isinstance(block, ToolUseBlock)
    }
    assert replay_ids == {f"call-{index}" for index in range(3, 8)}
    assert active.memory.through_transcript_sequence == 6
    state = store.get_compaction_state("run-1")
    assert state is not None
    assert state.reactive_round == 1
    assert state.consecutive_failures == 0


@pytest.mark.asyncio
async def test_reactive_compact_default_tail_uses_policy_value(store) -> None:
    """A reactive compact with no explicit tail size uses the policy setting."""
    store.create_investigation(investigation())
    seed_groups(store, count=8)  # 8 pairs: sequences 1-16
    compactor = RecordingCompactor(memory_with(evidence_ids=("ev-1",)), adapt=True)
    manager_ = AgentContextManager(
        store,
        compactor=compactor,
        policy=ContextBudgetPolicy(reactive_keep_recent_groups=3),
        now=lambda: NOW,
    )
    run = run_with("ev-1")
    active = await manager_.reactive_compact(run)
    assert active.memory is not None
    # head (first 5 pairs) is compacted; tail (last 3 pairs) is replayed whole.
    replay_ids = {
        block.tool_call_id
        for message in active.messages
        for block in message.blocks
        if isinstance(block, ToolUseBlock)
    }
    assert replay_ids == {f"call-{index}" for index in range(5, 8)}
    assert active.memory.through_transcript_sequence == 10


@pytest.mark.asyncio
async def test_reactive_compact_refuses_second_attempt_in_same_round(store) -> None:
    store.create_investigation(investigation())
    seed_groups(store, count=8)
    compactor = RecordingCompactor(memory_with(evidence_ids=("ev-1",)), adapt=True)
    manager_ = manager(store, compactor=compactor)
    run = run_with("ev-1")
    await manager_.reactive_compact(run, keep_recent_groups=5)
    with pytest.raises(CompactionRejected, match="reactive"):
        await manager_.reactive_compact(run, keep_recent_groups=5)
    assert len(compactor.requests) == 1


@pytest.mark.asyncio
async def test_reactive_compact_never_goes_backward_after_deterministic_boundary(
    store,
) -> None:
    """Reactive compaction reads after the latest boundary, never re-summarizing.

    A deterministic compact already summarized call-0..call-4 (boundary at
    sequence 10, which falls *inside* the tail a full-transcript split would
    preserve).  Reactive compaction must not build a memory revision whose
    coverage trails that boundary, must not insert a boundary row behind it, and
    must not replay the pre-boundary groups the prior memory already covers.
    """
    store.create_investigation(investigation())
    seed_groups(store, count=8)  # call-0..call-7, sequences 1-16
    prior_memory = memory_with(
        evidence_ids=("ev-1",),
        through_transcript_sequence=10,
        revision=1,
        memory_id="mem-run-1-1",
    )
    store.append_session_memory(prior_memory)
    store.append_compact_boundary(
        CompactBoundary(
            agent_run_id="run-1",
            through_sequence=10,
            memory_revision=1,
            summary="deterministic compact through sequence 10",
            created_at=NOW,
        )
    )
    compactor = RecordingCompactor(memory_with(evidence_ids=("ev-1",)), adapt=True)
    run = run_with("ev-1")
    active = await manager(store, compactor=compactor).reactive_compact(
        run, keep_recent_groups=5
    )
    # No out-of-order boundary row was inserted behind the deterministic one.
    assert boundary_sequences(store, "run-1") == {10}
    # The memory coverage never went backward (no re-summarized revision < 10).
    assert active.memory is not None
    assert active.memory.through_transcript_sequence == 10
    assert len(store.list_session_memories("run-1")) == 1
    # Only the post-boundary groups are replayed, preserved whole.
    replay_ids = {
        block.tool_call_id
        for message in active.messages
        for block in message.blocks
        if isinstance(block, ToolUseBlock)
    }
    assert replay_ids == {f"call-{index}" for index in range(5, 8)}
