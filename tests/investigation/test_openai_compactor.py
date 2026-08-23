"""Tests for the tool-free OpenAI-compatible compactor adapter.

Covers: payload construction (no executable tools), strict SessionMemory
response parsing, malformed/empty provider shapes, transport error redaction,
code-fence stripping, bounded JSON assertion, and request self-containedness.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from incidentlens_control_plane.investigation.compactor import (
    CompactionRejected,
    CompactionRequest,
)
from incidentlens_control_plane.investigation.context import AgentContextManager
from incidentlens_control_plane.investigation.model_transport import (
    ModelTransportError,
    OpenAICompatibleConfig,
)
from incidentlens_control_plane.investigation.openai_compactor import (
    OpenAICompatibleCompactor,
    _strip_fence,
)
from incidentlens_control_plane.investigation.provider import PromptTooLongError
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    CompactBoundary,
    EvidenceReference,
    MessageRole,
    SessionMemory,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope

NOW = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Records payloads and returns a canned response or raises a canned error."""

    def __init__(
        self,
        *,
        response: dict[str, object] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    def chat_completions(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _response(payload: dict[str, object]) -> dict[str, object]:
    """Wrap a SessionMemory-like dict in a fake model response envelope."""
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(payload, default=str),
                },
            }
        ]
    }


def _full_memory_payload() -> dict[str, object]:
    """A complete SessionMemory payload that satisfies strict validation."""
    return {
        "memory_id": "mem-run-1-1",
        "agent_run_id": "run-1",
        "investigation_id": "inv-1",
        "revision": 1,
        "through_round": 1,
        "through_transcript_sequence": 1,
        "objective": "find the root cause of checkout 502s",
        "confirmed_facts": ["checkout requests return 502 after retries"],
        "active_hypotheses": ["payment gateway timeout"],
        "open_questions": ["when did 502s start?"],
        "completed_actions": ["checked order-service logs"],
        "child_findings": [],
        "evidence_ids": [],
        "user_constraints": [],
        "todos": ["verify payment gateway health"],
        "next_actions": ["check payment gateway SLAs"],
        "created_at": NOW.isoformat(),
    }


def _sample_transcript() -> tuple[TranscriptMessage, ...]:
    return (
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=1,
            role=MessageRole.ASSISTANT,
            blocks=[
                ToolUseBlock(
                    tool_call_id="call-1",
                    tool_name="search_logs",
                    arguments={"query": "502"},
                ),
            ],
            created_at=NOW,
        ),
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=2,
            role=MessageRole.USER,
            blocks=[
                ToolResultBlock(
                    tool_call_id="call-1",
                    status="succeeded",
                    content="Found 3 matches",
                    evidence_ids=("ev-1",),
                ),
            ],
            created_at=NOW,
        ),
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=3,
            role=MessageRole.ASSISTANT,
            blocks=[
                TextBlock(text="The root cause is a payment gateway timeout."),
            ],
            created_at=NOW,
        ),
    )


@pytest.fixture
def config() -> OpenAICompatibleConfig:
    return OpenAICompatibleConfig(
        api_key="test-key",
        base_url="https://llm.example.com/v1",
        model="spark-x",
    )


@pytest.fixture
def compact_request() -> CompactionRequest:
    return CompactionRequest(
        agent_run_id="run-1",
        investigation_id="inv-1",
        through_round=1,
        through_sequence=3,
        messages=_sample_transcript(),
        allowed_evidence_ids=("ev-1",),
    )


# ---------------------------------------------------------------------------
# Brief Step 1: adapter contract tests with mocked HTTP boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compactor_sends_no_executable_tools(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    transport = _FakeTransport(response=_response(memory_payload))
    compactor = OpenAICompatibleCompactor(config, transport=transport)
    memory = await compactor.compact(compact_request)
    payload = transport.calls[0]
    assert payload.get("tools", []) == []
    assert payload["response_format"] == {"type": "json_object"}
    assert memory.agent_run_id == compact_request.agent_run_id


@pytest.mark.asyncio
async def test_compactor_rejects_malformed_provider_shape(
    config: OpenAICompatibleConfig, compact_request: CompactionRequest
) -> None:
    compactor = OpenAICompatibleCompactor(
        config, transport=_FakeTransport(response={"choices": []})
    )
    with pytest.raises(CompactionRejected, match="invalid"):
        await compactor.compact(compact_request)


# ---------------------------------------------------------------------------
# Strict identity payload: the adapter never repairs model output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compactor_does_not_repair_wrong_identity(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    """A wrong echoed identity must pass through unchanged for the manager to reject."""
    memory_payload = {**memory_payload, "agent_run_id": "other-run"}
    compactor = OpenAICompatibleCompactor(
        config, transport=_FakeTransport(response=_response(memory_payload))
    )
    memory = await compactor.compact(compact_request)
    assert memory.agent_run_id == "other-run"


@pytest.mark.asyncio
async def test_compactor_does_not_repair_foreign_evidence(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    """Foreign evidence is not filtered by the adapter; the validator rejects it."""
    memory_payload = {**memory_payload, "evidence_ids": ["foreign"]}
    compactor = OpenAICompatibleCompactor(
        config, transport=_FakeTransport(response=_response(memory_payload))
    )
    memory = await compactor.compact(compact_request)
    assert memory.evidence_ids == ("foreign",)


@pytest.mark.asyncio
async def test_compactor_requires_full_strict_memory_shape(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    """A payload missing a mandatory semantic field is rejected, never filled in."""
    del memory_payload["objective"]
    compactor = OpenAICompatibleCompactor(
        config, transport=_FakeTransport(response=_response(memory_payload))
    )
    with pytest.raises(CompactionRejected, match="invalid"):
        await compactor.compact(compact_request)


@pytest.mark.asyncio
async def test_compactor_deterministically_bounds_overlong_text_items(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    """Provider prose is bounded before strict semantic preservation checks."""
    memory_payload = {
        **memory_payload,
        "completed_actions": ["x" * 241],
    }
    compactor = OpenAICompatibleCompactor(
        config, transport=_FakeTransport(response=_response(memory_payload))
    )

    memory = await compactor.compact(compact_request)

    assert memory.completed_actions == ("x" * 240,)


# ---------------------------------------------------------------------------
# Brief Step 3: CompactionRequest self-containedness
# ---------------------------------------------------------------------------


def test_compaction_request_carries_investigation_identity(
    compact_request: CompactionRequest,
) -> None:
    """The request carries investigation_id and through_round for the compactor."""
    assert compact_request.investigation_id == "inv-1"
    assert compact_request.through_round == 1
    assert compact_request.through_sequence == 3
    assert compact_request.agent_run_id == "run-1"


# ---------------------------------------------------------------------------
# Brief Step 5: transport + redaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compactor_maps_429_to_rejected_without_response_body(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
) -> None:
    error = ModelTransportError(
        "OpenAI-compatible API 请求失败（HTTP 429）",
        retryable=True,
        category="http_error",
    )
    compactor = OpenAICompatibleCompactor(config, transport=_FakeTransport(error=error))
    with pytest.raises(CompactionRejected) as excinfo:
        await compactor.compact(compact_request)
    assert "secret provider body" not in str(excinfo.value)
    assert "429" in str(excinfo.value)


@pytest.mark.asyncio
async def test_compactor_maps_connection_failure_to_rejected(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
) -> None:
    error = ModelTransportError(
        "OpenAI-compatible API 连接失败", retryable=True, category="connection"
    )
    compactor = OpenAICompatibleCompactor(config, transport=_FakeTransport(error=error))
    with pytest.raises(CompactionRejected) as excinfo:
        await compactor.compact(compact_request)
    assert "secret connection detail" not in str(excinfo.value)
    assert "连接失败" in str(excinfo.value)


@pytest.mark.asyncio
async def test_compactor_maps_prompt_too_long_to_rejected(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
) -> None:
    compactor = OpenAICompatibleCompactor(
        config, transport=_FakeTransport(error=PromptTooLongError())
    )
    with pytest.raises(CompactionRejected, match="too long"):
        await compactor.compact(compact_request)


# ---------------------------------------------------------------------------
# _strip_fence
# ---------------------------------------------------------------------------


def test_strip_fence_removes_markdown_wrapper() -> None:
    raw = '```json\n{"key": "value"}\n```'
    assert _strip_fence(raw) == '{"key": "value"}'


def test_strip_fence_passes_through_plain_json() -> None:
    raw = '{"key": "value"}'
    assert _strip_fence(raw) == '{"key": "value"}'


def test_strip_fence_rejects_non_string() -> None:
    with pytest.raises(CompactionRejected, match="string"):
        _strip_fence(42)


# ---------------------------------------------------------------------------
# Payload structure
# ---------------------------------------------------------------------------


def test_compaction_payload_has_system_and_user_messages(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
) -> None:
    from incidentlens_control_plane.investigation.openai_compactor import (
        _compaction_messages,
    )

    messages = _compaction_messages(compact_request)
    roles = [m["role"] for m in messages]
    assert roles[0] == "system"
    assert all(r == "user" for r in roles[1:])


def test_expected_output_includes_identity_fields(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
) -> None:
    from incidentlens_control_plane.investigation.openai_compactor import (
        _serialize_expected_output,
    )

    expected = json.loads(_serialize_expected_output(compact_request))
    assert expected["agent_run_id"] == "run-1"
    assert expected["investigation_id"] == "inv-1"
    assert expected["through_round"] == 1
    assert expected["through_transcript_sequence"] == 3
    assert expected["expected_revision"] == 1  # no prior memory


def test_expected_output_supplies_exact_session_memory_schema(
    compact_request: CompactionRequest,
) -> None:
    """Structured fields must not drift into objects DeepSeek cannot validate."""
    from incidentlens_control_plane.investigation.openai_compactor import (
        _serialize_expected_output,
    )

    expected = json.loads(_serialize_expected_output(compact_request))
    schema = expected["session_memory_json_schema"]
    properties = schema["properties"]

    assert properties["safety_state"]["type"] == "array"
    assert properties["safety_state"]["items"]["type"] == "string"
    assert properties["todos"]["items"]["type"] == "string"
    recipe = schema["$defs"]["ReacquisitionRecipe"]
    assert "arguments" in recipe["properties"]
    assert "redacted_arguments" not in recipe["properties"]
    assert expected["max_chars_per_text_item"]["completed_actions"] == 240
    assert expected["max_chars_per_text_item"]["todos"] == 1_000


def test_expected_output_names_pending_call_ids_that_must_survive(
    compact_request: CompactionRequest,
) -> None:
    """A summary must receive the literal IDs the preservation gate checks."""
    from incidentlens_control_plane.investigation.openai_compactor import (
        _serialize_expected_output,
    )

    failed = TranscriptMessage(
        agent_run_id="run-1",
        sequence=4,
        role=MessageRole.USER,
        blocks=[
            ToolResultBlock(
                tool_call_id="call-pending-7",
                status=ToolCallStatus.UNCERTAIN,
                content="execution state unknown",
            )
        ],
        created_at=NOW,
    )
    request = compact_request.model_copy(
        update={"messages": (*compact_request.messages, failed)}
    )

    expected = json.loads(_serialize_expected_output(request))
    assert expected["must_preserve"]["pending_tool_call_ids"] == [
        "call-pending-7"
    ]


@pytest.fixture
def memory_payload() -> dict[str, object]:
    return _full_memory_payload()


# ---------------------------------------------------------------------------
# Preservation contract: evidence-backed state must survive a summary
# ---------------------------------------------------------------------------


def _result_block(
    call_id: str,
    *,
    status=ToolCallStatus.SUCCEEDED,
    content: str,
    persisted_output: bool = False,
) -> ToolResultBlock:
    return ToolResultBlock(
        tool_call_id=call_id,
        status=status,
        content=content,
        evidence_ids=("ev-1",) if status is ToolCallStatus.SUCCEEDED else (),
        persisted_output=persisted_output,
    )


def _extended_transcript(
    *blocks: ToolUseBlock | ToolResultBlock,
) -> tuple[TranscriptMessage, ...]:
    """The sample transcript plus one user message at sequence 4."""
    return _sample_transcript() + (
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=4,
            role=MessageRole.USER,
            blocks=blocks,
            created_at=NOW,
        ),
    )


@pytest.mark.asyncio
async def test_compactor_rejects_dropped_evidence_hash(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    transcript = _extended_transcript(
        _result_block("call-2", content="observed target sha256 " + "a" * 64,
                      persisted_output=True)
    )
    request = compact_request.model_copy(
        update={"through_sequence": 4, "messages": transcript}
    )
    compactor = OpenAICompatibleCompactor(
        config, transport=_FakeTransport(response=_response(memory_payload))
    )
    with pytest.raises(CompactionRejected, match="hash"):
        await compactor.compact(request)


@pytest.mark.asyncio
async def test_compactor_accepts_carried_evidence_hash(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    transcript = _extended_transcript(
        _result_block("call-2", content="observed target sha256 " + "a" * 64,
                      persisted_output=True)
    )
    request = compact_request.model_copy(
        update={"through_sequence": 4, "messages": transcript}
    )
    memory_payload = {
        **memory_payload,
        "immutable_observations": ["pre-change target sha256 " + "a" * 64],
    }
    compactor = OpenAICompatibleCompactor(
        config, transport=_FakeTransport(response=_response(memory_payload))
    )
    memory = await compactor.compact(request)
    assert "a" * 64 in " ".join(memory.immutable_observations)


@pytest.mark.asyncio
async def test_compactor_rejects_dropped_pending_approval(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    transcript = _extended_transcript(
        _result_block("call-2", status=ToolCallStatus.WAITING_APPROVAL,
                      content="apply restart after approval")
    )
    request = compact_request.model_copy(
        update={"through_sequence": 4, "messages": transcript}
    )
    compactor = OpenAICompatibleCompactor(
        config, transport=_FakeTransport(response=_response(memory_payload))
    )
    with pytest.raises(CompactionRejected, match="pending"):
        await compactor.compact(request)


@pytest.mark.asyncio
async def test_compactor_accepts_carried_pending_approval(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    transcript = _extended_transcript(
        _result_block("call-2", status=ToolCallStatus.WAITING_APPROVAL,
                      content="apply restart after approval")
    )
    request = compact_request.model_copy(
        update={"through_sequence": 4, "messages": transcript}
    )
    memory_payload = {
        **memory_payload,
        "pending_actions": ["call-2 waiting approval to restart orders"],
        "safety_state": ["call-2 approval pending; no change applied"],
    }
    compactor = OpenAICompatibleCompactor(
        config, transport=_FakeTransport(response=_response(memory_payload))
    )
    memory = await compactor.compact(request)
    assert any("call-2" in text for text in (*memory.safety_state, *memory.pending_actions))


@pytest.mark.asyncio
async def test_compactor_rejects_dropped_verification_state(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    transcript = _extended_transcript(
        ToolUseBlock(
            tool_call_id="call-2",
            tool_name="verify_service_health",
            arguments={"service_name": "orders"},
        )
    )
    request = compact_request.model_copy(
        update={"messages": transcript}
    )
    compactor = OpenAICompatibleCompactor(
        config, transport=_FakeTransport(response=_response(memory_payload))
    )
    with pytest.raises(CompactionRejected, match="verification"):
        await compactor.compact(request)


@pytest.mark.asyncio
async def test_compactor_requires_concrete_next_actions(
    config: OpenAICompatibleConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    memory_payload = {**memory_payload, "next_actions": []}
    compactor = OpenAICompatibleCompactor(
        config, transport=_FakeTransport(response=_response(memory_payload))
    )
    with pytest.raises(CompactionRejected, match="next_actions"):
        await compactor.compact(compact_request)


# ---------------------------------------------------------------------------
# Failed compaction safety: a rejected compact never advances the boundary
# ---------------------------------------------------------------------------


def _compaction_store(tmp_path) -> InvestigationStore:
    factory = lambda: sqlite3.connect(tmp_path / "compaction.db")  # noqa: E731
    store = InvestigationStore(factory)
    store.migrate()
    return store


def _run_with_evidence() -> AgentRun:
    return AgentRun(
        agent_run_id="run-1",
        investigation_id="inv-1",
        kind=AgentRunKind.PARENT,
        scope=AgentScope(
            project_id="checkout", target_id="prod-a", scope=LogScope.HOST
        ),
        status=AgentRunStatus.RUNNING,
        budget=AgentBudget(),
        usage=UsageCounters(rounds=1),
        evidence=(
            EvidenceReference(
                evidence_id="ev-1",
                operation_id="op-ev-1",
                summary="output of ev-1",
            ),
        ),
        created_at=NOW,
        updated_at=NOW,
    )


def _seed_groups(store: InvestigationStore, *, count: int = 4) -> None:
    """Append ``count`` assistant tool-use/tool-result pairs (sequences 1..2n)."""
    for index in range(count):
        sequence = index * 2 + 1
        store.append_transcript_message(
            TranscriptMessage(
                agent_run_id="run-1",
                sequence=sequence,
                role=MessageRole.ASSISTANT,
                blocks=[
                    ToolUseBlock(
                        tool_call_id=f"call-{index}",
                        tool_name="logs.query",
                        arguments={},
                    ),
                ],
                created_at=NOW,
            )
        )
        store.append_transcript_message(
            TranscriptMessage(
                agent_run_id="run-1",
                sequence=sequence + 1,
                role=MessageRole.USER,
                blocks=[
                    _result_block(f"call-{index}", content=f"result {index}"),
                ],
                created_at=NOW,
            )
        )


def _seed_previous_boundary(
    store: InvestigationStore,
) -> tuple[SessionMemory, CompactBoundary]:
    previous = SessionMemory(
        memory_id="mem-run-1-1",
        agent_run_id="run-1",
        investigation_id="inv-1",
        revision=1,
        through_round=1,
        through_transcript_sequence=4,
        objective="find the root cause of checkout 502s",
        evidence_ids=("ev-1",),
        next_actions=("verify target health",),
        created_at=NOW,
    )
    boundary = CompactBoundary(
        agent_run_id="run-1",
        through_sequence=4,
        memory_revision=1,
        summary="previous valid boundary",
        created_at=NOW,
    )
    store.append_session_memory(previous)
    store.append_compact_boundary(boundary)
    return previous, boundary


@pytest.mark.asyncio
async def test_failed_compaction_keeps_previous_boundary_and_trips_breaker(
    tmp_path,
    config: OpenAICompatibleConfig,
) -> None:
    """Retryable and non-retryable transport failures leave the boundary intact.

    Both a retryable HTTP failure and a non-retryable TLS certificate failure
    surface as ``CompactionRejected``; the manager advances the durable breaker
    while the previous memory and boundary stay untouched.  A later successful
    manual compaction recovers the boundary and resets the breaker.
    """
    store = _compaction_store(tmp_path)
    _seed_groups(store, count=4)  # sequences 1-8; boundary at 4 leaves a tail
    previous, previous_boundary = _seed_previous_boundary(store)
    run = _run_with_evidence()

    failures = (
        ModelTransportError(
            "OpenAI-compatible API 请求失败（HTTP 503）",
            retryable=True,
            category="http_error",
        ),
        ModelTransportError(
            "OpenAI-compatible API TLS 证书校验失败",
            retryable=False,
            category="tls_configuration",
        ),
    )
    for failure in failures:
        compactor = OpenAICompatibleCompactor(
            config, transport=_FakeTransport(error=failure)
        )
        manager_ = AgentContextManager(store, compactor=compactor, now=lambda: NOW)
        with pytest.raises(CompactionRejected):
            await manager_.semantic_compact(run)
        assert store.get_latest_compact_boundary("run-1") == previous_boundary
        assert store.get_latest_session_memory("run-1") == previous
    state = store.get_compaction_state("run-1")
    assert state is not None and state.consecutive_failures == 2

    # A third automatic attempt fails again and advances the breaker toward the
    # threshold; the boundary is still untouched.
    with pytest.raises(CompactionRejected):
        await manager_.semantic_compact(run)
    assert store.get_compaction_state("run-1").consecutive_failures == 3
    assert store.get_latest_compact_boundary("run-1") == previous_boundary
    assert store.get_latest_session_memory("run-1") == previous

    payload = {
        **_full_memory_payload(),
        "revision": 2,
        "memory_id": "mem-run-1-2",
        "through_transcript_sequence": 8,
        "through_round": 1,
        "evidence_ids": ["ev-1"],
    }
    good = OpenAICompatibleCompactor(
        config, transport=_FakeTransport(response=_response(payload))
    )
    manager_ = AgentContextManager(store, compactor=good, now=lambda: NOW)
    memory = await manager_.semantic_compact(run, manual=True)
    assert memory.revision == 2
    assert memory.through_transcript_sequence == 8
    boundary = store.get_latest_compact_boundary("run-1")
    assert boundary is not None and boundary.through_sequence == 8
    assert boundary.memory_revision == 2
    assert store.get_compaction_state("run-1").consecutive_failures == 0
    # The breaker reset: a subsequent automatic compact no longer trips it.
    recovered = await manager_.semantic_compact(run)
    assert recovered.revision == 2
