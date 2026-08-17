# Claude-Style Context and Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-round state snapshots with an append-only message history, bounded active context, persistent work plan, layered compaction, and one-shot prompt-too-long recovery.

**Architecture:** SQLite keeps the complete model-visible transcript, compact boundaries, session-memory revisions, and work-plan state. `AgentContextManager` materializes a bounded message tail by applying tool-result budgeting, group-safe snipping, micro-compaction, deterministic session memory, and optional semantic compaction; the orchestrator appends every assistant/tool exchange before continuing the existing guarded execution loop.

**Tech Stack:** Python 3.12, Pydantic 2, SQLite, asyncio, FastAPI, pytest, pytest-asyncio

## Global Constraints

- Implement only single-investigation and child-run context continuity; do not implement cross-incident Project Memory.
- Preserve existing Investigation, AgentRun, Evidence, ToolCall, approval, scope, and recovery safety boundaries.
- Store the exact model-visible transcript append-only; keep complete tool output in EvidenceStore and reference it from transcript blocks.
- Never split an assistant tool request from its matching tool result during compaction.
- Run cheap deterministic compaction before semantic compaction.
- Semantic compaction has no tools and may not create facts or evidence IDs.
- Retry prompt-too-long at most once per model turn; a second failure safely pauses the run.
- Keep all existing user worktree changes and migrate the current uncommitted context prototype forward instead of reverting it.

---

## File Structure

- `investigation/types.py`: transcript, content-block, compact-boundary, work-plan, memory, and budget contracts.
- `investigation/store.py`: SQLite persistence for transcript entries, boundaries, plan items, compaction state, and memory revisions.
- `investigation/transcript.py`: message grouping, tool-use/result pairing, and append-only transcript operations.
- `investigation/context.py`: token estimation, active-context materialization, deterministic compression, and post-compact restoration.
- `investigation/compactor.py`: semantic compactor contract, schema validation, and failure circuit breaker.
- `investigation/provider.py`: conversation request contract and typed prompt-too-long error.
- `investigation/xfyun_provider.py`: mapping active messages to the OpenAI-compatible API and provider error classification.
- `investigation/fake_provider.py`: scripted conversation provider and compactor responses for deterministic tests.
- `investigation/tools.py`: `todo_write` schema and concurrency metadata.
- `investigation/tool_executor.py`: persistent `todo_write` handler and execution-result-to-message conversion support.
- `investigation/orchestrator.py`: continuous message loop, transcript writes, compaction, reactive retry, and child-report delivery.
- `investigation/recovery.py`: transcript/plan validation during restart without replaying completed mutations.
- `config.py` and `runtime.py`: context-window, output-reserve, compaction, transcript, and retry wiring.
- `routes/investigations.py`: read-only transcript, memory, and work-plan inspection endpoints.
- `tests/investigation/test_transcript.py`: transcript persistence and pairing invariants.
- `tests/investigation/test_context.py`: layered context materialization and token budget behavior.
- `tests/investigation/test_compactor.py`: semantic compactor validation and breaker behavior.
- `tests/investigation/test_orchestrator.py`: end-to-end loop, Todo, reactive retry, and child isolation.
- `tests/investigation/test_recovery.py`: restart reconstruction and no-replay guarantees.
- `tests/investigation/test_xfyun_provider.py`: message mapping and prompt-too-long classification.
- `tests/web/test_investigations_api.py`: transcript, memory, and plan inspection APIs.

---

### Task 1: Persist the Model-Visible Transcript and Work Plan

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/types.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/store.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/transcript.py`
- Create: `tests/investigation/test_transcript.py`
- Modify: `tests/investigation/test_store.py`

**Interfaces:**
- Produces: `TranscriptMessage`, `TextBlock`, `ToolUseBlock`, `ToolResultBlock`, `CompactBoundary`, `TodoItem`, `CompactionState`.
- Produces: `InvestigationStore.append_transcript_message()`, `list_transcript_messages()`, `append_compact_boundary()`, `get_latest_compact_boundary()`, `replace_todos()`, `list_todos()`, `get_compaction_state()`, `put_compaction_state()`.
- Produces: `TranscriptService.append_message()` and `TranscriptService.group_messages()` for later context materialization.

- [ ] **Step 1: Write failing transcript and plan persistence tests**

```python
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
        blocks=(ToolUseBlock(tool_call_id="call-1", tool_name="registry_info", arguments={}),),
        created_at=NOW,
    )
    store.append_transcript_message(first)
    store.append_transcript_message(second)
    assert store.list_transcript_messages("run-1") == (first, second)
    with pytest.raises(TranscriptConflict):
        store.append_transcript_message(second)


def test_work_plan_has_at_most_one_in_progress_item(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="at most one"):
        store.replace_todos(
            "run-1",
            (
                TodoItem(todo_id="one", content="inspect logs", status=TodoStatus.IN_PROGRESS, updated_at=NOW),
                TodoItem(todo_id="two", content="check database", status=TodoStatus.IN_PROGRESS, updated_at=NOW),
            ),
        )
```

- [ ] **Step 2: Run the new tests and verify the contracts are missing**

Run: `uv run pytest tests/investigation/test_transcript.py tests/investigation/test_store.py -q`

Expected: FAIL during import because transcript and Todo contracts do not exist.

- [ ] **Step 3: Add discriminated content blocks and durable state contracts**

```python
class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class TextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=200_000)


class ToolUseBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["tool_use"] = "tool_use"
    tool_call_id: str = Field(min_length=1, max_length=120)
    tool_name: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str = Field(min_length=1, max_length=120)
    status: ToolCallStatus
    content: str = Field(max_length=200_000)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=100)
    persisted_output: bool = False


MessageBlock = Annotated[
    TextBlock | ToolUseBlock | ToolResultBlock,
    Field(discriminator="type"),
]


class TranscriptMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_run_id: str = Field(min_length=1, max_length=120)
    sequence: int = Field(ge=1)
    role: MessageRole
    blocks: tuple[MessageBlock, ...] = Field(min_length=1)
    created_at: datetime


class TodoStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class TodoItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    todo_id: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=1_000)
    status: TodoStatus
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=32)
    tool_call_ids: tuple[str, ...] = Field(default=(), max_length=32)
    updated_at: datetime
```

Add `CompactBoundary` with `through_sequence`, `memory_revision`, `summary`, and timestamps. Add `CompactionState` with `consecutive_failures`, `reactive_round`, and `latest_boundary_sequence`. New `SessionMemory` fields must have backward-compatible defaults (`through_transcript_sequence=0` and empty tuples) so rows created by the current prototype remain readable during migration.

- [ ] **Step 4: Add SQLite tables and append/read methods**

```sql
CREATE TABLE IF NOT EXISTS agent_transcript_messages (
    agent_run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (agent_run_id, sequence)
);
CREATE TABLE IF NOT EXISTS agent_compact_boundaries (
    agent_run_id TEXT NOT NULL,
    through_sequence INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (agent_run_id, through_sequence)
);
CREATE TABLE IF NOT EXISTS agent_todos (
    agent_run_id TEXT NOT NULL,
    todo_id TEXT NOT NULL,
    record_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent_run_id, todo_id)
);
CREATE TABLE IF NOT EXISTS agent_compaction_state (
    agent_run_id TEXT PRIMARY KEY,
    record_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

`replace_todos()` must validate all items first, then replace the run's rows in one transaction. Transcript and boundary inserts must translate duplicate keys into `TranscriptConflict` and `CompactBoundaryConflict`.

- [ ] **Step 5: Implement message grouping and pairing validation**

```python
@dataclass(frozen=True, slots=True)
class MessageGroup:
    messages: tuple[TranscriptMessage, ...]


def group_messages(messages: tuple[TranscriptMessage, ...]) -> tuple[MessageGroup, ...]:
    groups: list[MessageGroup] = []
    index = 0
    while index < len(messages):
        current = messages[index]
        tool_ids = {
            block.tool_call_id for block in current.blocks if isinstance(block, ToolUseBlock)
        }
        if tool_ids:
            if index + 1 >= len(messages):
                raise UnpairedToolMessage("tool use has no following result")
            following = messages[index + 1]
            result_ids = {
                block.tool_call_id for block in following.blocks if isinstance(block, ToolResultBlock)
            }
            if result_ids != tool_ids:
                raise UnpairedToolMessage("tool result ids do not match tool use ids")
            groups.append(MessageGroup((current, following)))
            index += 2
            continue
        groups.append(MessageGroup((current,)))
        index += 1
    return tuple(groups)
```

- [ ] **Step 6: Run persistence and pairing tests**

Run: `uv run pytest tests/investigation/test_transcript.py tests/investigation/test_store.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation/types.py apps/control-plane/src/incidentlens_control_plane/investigation/store.py apps/control-plane/src/incidentlens_control_plane/investigation/transcript.py tests/investigation/test_transcript.py tests/investigation/test_store.py
git commit -m "feat(agent): persist transcript and work plan"
```

---

### Task 2: Change Model Calls from Snapshots to Conversations

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/provider.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/xfyun_provider.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/fake_provider.py`
- Modify: `tests/investigation/test_fake_provider.py`
- Modify: `tests/investigation/test_xfyun_provider.py`

**Interfaces:**
- Consumes: `TranscriptMessage` from Task 1.
- Produces: `ConversationRequest`, `PromptTooLongError`, `ModelProvider.generate_turn(ConversationRequest)`.
- Preserves: `AgentTurnResult` as the validated domain action result, so existing Guard and orchestration validation remain applicable.

- [ ] **Step 1: Write failing conversation mapping tests**

```python
@pytest.mark.asyncio
async def test_provider_receives_continuous_messages() -> None:
    registry = FakeProviderRegistry()
    registry.script("run-1", [StopStep(stop_signal=completed_stop())])
    request = conversation_request(
        messages=(
            user_message(1, "inspect checkout"),
            assistant_tool_message(2, "call-1", "registry_info"),
            tool_result_message(3, "call-1", "service orders"),
        )
    )
    await FakeProvider(registry).generate_turn(request)
    assert registry.requests("run-1")[0].messages == request.messages


def test_http_413_is_prompt_too_long(provider_config) -> None:
    provider = XfyunMaaSProvider(provider_config)
    with patch("incidentlens_control_plane.investigation.xfyun_provider.urlopen", side_effect=http_error(413)):
        with pytest.raises(PromptTooLongError):
            provider._post({"messages": []})
```

- [ ] **Step 2: Run provider tests and verify failure**

Run: `uv run pytest tests/investigation/test_fake_provider.py tests/investigation/test_xfyun_provider.py -q`

Expected: FAIL because `ConversationRequest` and `PromptTooLongError` are missing.

- [ ] **Step 3: Introduce the conversation request without weakening output validation**

```python
class ConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    checkpoint: RunCheckpoint
    investigation: InvestigationSnapshot
    task_prompt: str | None = Field(default=None, min_length=1, max_length=4_000)
    messages: tuple[TranscriptMessage, ...]
    tool_schemas: tuple[ToolSchema, ...] = Field(default=(), max_length=32)


class PromptTooLongError(ProviderError):
    def __init__(self, message: str = "model context is too long") -> None:
        super().__init__(message, retryable=False)
```

Keep `ProviderOutputValidator` evidence ownership checks. It must validate proposed evidence IDs against the run's durable evidence, not only against the compacted message tail.

- [ ] **Step 4: Map transcript blocks to OpenAI-compatible messages**

```python
def _message_payload(message: TranscriptMessage) -> dict[str, object]:
    if message.role is MessageRole.ASSISTANT:
        return {"role": "assistant", "content": _assistant_content(message.blocks)}
    return {"role": "user", "content": _user_content(message.blocks)}


payload = {
    "model": self._config.model,
    "temperature": 0.1,
    "response_format": {"type": "json_object"},
    "messages": [
        {"role": "system", "content": _SYSTEM_PROMPT},
        *_context_attachments(request),
        *(_message_payload(message) for message in request.messages),
    ],
    "tools": [_tool_payload(schema) for schema in request.tool_schemas],
}
```

Classify HTTP 413 and provider-specific `context_length_exceeded` responses as `PromptTooLongError`. Keep 408/429/5xx retry behavior unchanged.

- [ ] **Step 5: Update the fake provider registry to record full conversation requests**

Store received requests per run and continue returning the existing scripted `AgentTurnResult` objects. This preserves deterministic orchestrator tests while allowing assertions over active messages.

- [ ] **Step 6: Run provider tests**

Run: `uv run pytest tests/investigation/test_fake_provider.py tests/investigation/test_xfyun_provider.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation/provider.py apps/control-plane/src/incidentlens_control_plane/investigation/xfyun_provider.py apps/control-plane/src/incidentlens_control_plane/investigation/fake_provider.py tests/investigation/test_fake_provider.py tests/investigation/test_xfyun_provider.py
git commit -m "refactor(agent): send continuous model conversations"
```

---

### Task 3: Materialize a Token-Bounded Active Context

**Files:**
- Replace: `apps/control-plane/src/incidentlens_control_plane/investigation/context.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/types.py`
- Replace: `tests/investigation/test_context.py`

**Interfaces:**
- Consumes: transcript groups, latest boundary, Session Memory, Todo items, and tool schemas.
- Produces: `ContextBudget`, `TokenEstimator`, `ActiveContext`, `AgentContextManager.build()`.
- Produces: deterministic `tool_result_budget()`, `snip_groups()`, and `micro_compact()` transformations.

- [ ] **Step 1: Write failing layered-compaction tests**

```python
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
    active = manager(store, context_window=16_000, max_output_tokens=2_000, reserve_tokens=1_000).build(
        run(), investigation(), large_schemas()
    )
    assert active.budget.input_tokens <= 13_000
```

- [ ] **Step 2: Run context tests and verify failure**

Run: `uv run pytest tests/investigation/test_context.py -q`

Expected: FAIL because the current manager uses partial character counting and state snapshots.

- [ ] **Step 3: Implement explicit token budget contracts**

```python
@dataclass(frozen=True, slots=True)
class ContextBudget:
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


class TokenEstimator(Protocol):
    def count_text(self, text: str) -> int: ...
    def count_json(self, value: object) -> int: ...


class ConservativeTokenEstimator:
    def __init__(self, chars_per_token: float = 2.5) -> None:
        self._chars_per_token = chars_per_token

    def count_text(self, text: str) -> int:
        return max(1, math.ceil(len(text) / self._chars_per_token))
```

Include system prompt, serialized tool schemas, all active messages, context attachments, maximum output, and reserve. Expose calibration that lowers `chars_per_token` when actual provider input usage exceeds an estimate; never calibrate in the optimistic direction automatically.

- [ ] **Step 4: Implement the cheap compaction order**

`AgentContextManager.build()` must execute:

```python
groups = transcript.group_messages(run.agent_run_id, after=boundary.through_sequence if boundary else 0)
groups = tool_result_budget(groups, max_chars=policy.tool_result_budget_chars)
groups = snip_groups(groups, max_groups=policy.max_message_groups)
groups = micro_compact(groups, keep_recent=policy.keep_recent_tool_results)
messages = restore_context_header(run, investigation, memory, todos) + flatten(groups)
```

If the result exceeds `max_input_tokens`, build a deterministic Session Memory and rerun materialization from its boundary. Drop the oldest eligible recent groups only after Memory exists. Never drop the context header, Todo, pending approval, failed/uncertain result, or unmatched child notification.

- [ ] **Step 5: Extend Session Memory with exact coverage and work state**

Add `through_transcript_sequence`, `user_constraints`, `todos`, and `next_actions`. Build facts and evidence IDs only from current-run durable state. Stop using fixed `compact_every_rounds`; trigger from actual context pressure or explicit compact request.

- [ ] **Step 6: Run context tests and existing store tests**

Run: `uv run pytest tests/investigation/test_context.py tests/investigation/test_store.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation/context.py apps/control-plane/src/incidentlens_control_plane/investigation/types.py tests/investigation/test_context.py tests/investigation/test_store.py
git commit -m "feat(agent): materialize token-bounded context"
```

---

### Task 4: Add Semantic and Reactive Compaction

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/compactor.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/context.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/provider.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/fake_provider.py`
- Create: `tests/investigation/test_compactor.py`

**Interfaces:**
- Produces: `CompactionRequest`, `ContextCompactor.compact()`, `CompactionRejected`, `CompactionCircuitOpen`.
- Consumes: `SessionMemory`, transcript groups, durable run evidence IDs, and `CompactionState`.
- Produces: `AgentContextManager.semantic_compact()` and `reactive_compact(keep_recent_groups=5)`.

- [ ] **Step 1: Write failing semantic-compaction tests**

```python
@pytest.mark.asyncio
async def test_compactor_has_no_tools_and_preserves_owned_evidence(store) -> None:
    compactor = RecordingCompactor(memory_with(evidence_ids=("ev-1",)))
    memory = await manager(store, compactor=compactor).semantic_compact(run_with("ev-1"))
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
```

- [ ] **Step 2: Run compactor tests and verify failure**

Run: `uv run pytest tests/investigation/test_compactor.py -q`

Expected: FAIL because semantic compaction contracts do not exist.

- [ ] **Step 3: Implement a tool-free compactor contract and validation**

```python
class CompactionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_run_id: str
    through_sequence: int
    prior_memory: SessionMemory | None = None
    messages: tuple[TranscriptMessage, ...]
    allowed_evidence_ids: tuple[str, ...]


class ContextCompactor(Protocol):
    async def compact(self, request: CompactionRequest) -> SessionMemory: ...
```

`CompactionRequest` deliberately has no tool field. The production implementation must call the configured model with a text-only instruction and an empty tool list. Validate that returned evidence IDs are a subset of `allowed_evidence_ids`, the boundary is monotonic, required work-state fields are present, and all text fields pass existing redaction/length validation.

- [ ] **Step 4: Persist breaker state and compact boundaries**

Add `InvestigationStore.commit_compaction(memory, boundary, state)`. It must insert the Session Memory revision and compact boundary and update breaker state in one SQLite transaction; any conflict or write failure rolls back all three changes. On success reset `consecutive_failures` to zero. On invalid output or provider failure, persist the increment separately. Reject the fourth attempt until a successful manual compact or a new run resets the state.

- [ ] **Step 5: Implement reactive compaction preserving five complete groups**

```python
async def reactive_compact(self, run: AgentRun, *, keep_recent_groups: int = 5) -> ActiveContext:
    groups = self._transcript.group_messages(run.agent_run_id)
    head, tail = groups[:-keep_recent_groups], groups[-keep_recent_groups:]
    memory = await self._semantic_compact_groups(run, head)
    return self._materialize_after(memory.through_transcript_sequence, tail)
```

The method must refuse a second reactive attempt for the same round using `CompactionState.reactive_round`.

- [ ] **Step 6: Run compactor and context tests**

Run: `uv run pytest tests/investigation/test_compactor.py tests/investigation/test_context.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation/compactor.py apps/control-plane/src/incidentlens_control_plane/investigation/context.py apps/control-plane/src/incidentlens_control_plane/investigation/provider.py apps/control-plane/src/incidentlens_control_plane/investigation/fake_provider.py tests/investigation/test_compactor.py tests/investigation/test_context.py
git commit -m "feat(agent): add semantic and reactive compaction"
```

---

### Task 5: Add Todo and Manual Compact Harness Tools

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/tools.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/tool_executor.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/context.py`
- Modify: `tests/investigation/test_tool_executor.py`
- Modify: `tests/investigation/test_context.py`

**Interfaces:**
- Consumes: `TodoItem` and `InvestigationStore.replace_todos()` from Task 1.
- Produces: registered `todo_write` tool with input `{todos: [{todo_id, content, status, evidence_ids, tool_call_ids}]}`.
- Produces: registered `compact_context` control tool with an empty input object; the orchestrator handles it without remote execution.
- Produces: `concurrency_safe` metadata on tool definitions for Task 6 batching.
- Produces: tool result text `Updated N plan items` with no EvidenceReference.

- [ ] **Step 1: Write failing Todo tool tests**

```python
@pytest.mark.asyncio
async def test_todo_write_persists_plan_without_remote_execution(executor, store, run) -> None:
    outcome = await executor.execute(
        ToolRequest(
            tool_call_id="plan-1",
            tool_name="todo_write",
            arguments={"todos": [
                {"todo_id": "inspect", "content": "inspect order logs", "status": "in_progress"},
                {"todo_id": "verify", "content": "verify root cause", "status": "pending"},
            ]},
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert [item.todo_id for item in store.list_todos(run.agent_run_id)] == ["inspect", "verify"]
    assert outcome.evidence == ()


def test_compact_context_is_registered_as_local_control_tool(executor) -> None:
    schema = next(item for item in executor.tool_schemas() if item.tool_name == "compact_context")
    assert schema.parameters_json_schema == {"type": "object", "additionalProperties": False}
    assert schema.requires_approval is False
```

- [ ] **Step 2: Run tool tests and verify failure**

Run: `uv run pytest tests/investigation/test_tool_executor.py -q`

Expected: FAIL because `todo_write`, `compact_context`, and concurrency metadata are not registered.

- [ ] **Step 3: Register and implement the plan-only tool**

Define `TOOL_TODO_WRITE = "todo_write"` and a schema with required `todo_id`, `content`, and enum status. Mark it concurrency-safe because it only atomically replaces the current run's plan and does not touch remote state. The executor must attach `updated_at=now`, validate evidence ownership against the run, and call `replace_todos()`.

Add `concurrency_safe: bool = False` to `ToolSchema`. Mark only pure reads and `todo_write` safe. File mutations, shell execution, Docker actions, approvals, delegation, and `compact_context` remain serial.

- [ ] **Step 4: Restore Todo in every active context**

Serialize the current plan, recently owned evidence references, and latest bounded child reports into the fixed context header before Session Memory and recent transcript. Keep these restoration attachments outside snip and micro-compaction. Add the instruction that complex investigations create or update the plan before unrelated tools, but do not implement the teaching demo's fixed three-round nag.

- [ ] **Step 5: Route manual compact through the harness**

The executor must identify `compact_context` as a local control request and return a typed control outcome without executing remote code. Task 6 will call `AgentContextManager.semantic_compact(manual=True)`, reset the breaker after success, append a compact boundary, and return `[Context compacted]` as the matching tool result.

- [ ] **Step 6: Run harness-tool and context tests**

Run: `uv run pytest tests/investigation/test_tool_executor.py tests/investigation/test_context.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 5**

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation/tools.py apps/control-plane/src/incidentlens_control_plane/investigation/tool_executor.py apps/control-plane/src/incidentlens_control_plane/investigation/context.py tests/investigation/test_tool_executor.py tests/investigation/test_context.py
git commit -m "feat(agent): add plan and compact harness tools"
```

---

### Task 6: Integrate the Continuous Loop, Tool Messages, and Child Isolation

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/provider.py`
- Modify: `tests/investigation/test_orchestrator.py`
- Modify: `tests/integration/test_live_agent_runtime.py`

**Interfaces:**
- Consumes: `ConversationRequest`, `TranscriptService`, `AgentContextManager`, and `PromptTooLongError`.
- Produces: append-before-act orchestration and one-shot reactive retry.
- Preserves: Guard, approval, evidence, checkpoint, child semaphore, cancellation, and no-replay behavior.

- [ ] **Step 1: Write failing loop continuity and retry tests**

```python
@pytest.mark.asyncio
async def test_tool_result_is_in_next_model_conversation(runtime) -> None:
    runtime.fake.script("run-1", [request_registry_info(), completed_step("ev-1")])
    await runtime.orchestrator.run("run-1")
    second = runtime.fake.requests("run-1")[1]
    assert any(
        isinstance(block, ToolResultBlock) and block.tool_call_id == "registry-call"
        for message in second.messages for block in message.blocks
    )


@pytest.mark.asyncio
async def test_prompt_too_long_compacts_once_then_retries(runtime) -> None:
    runtime.fake.script("run-1", [PromptTooLongError(), completed_step("ev-1")])
    run = await runtime.orchestrator.run("run-1")
    assert run.status is AgentRunStatus.COMPLETED
    assert runtime.fake.call_count("run-1") == 2


@pytest.mark.asyncio
async def test_second_prompt_too_long_pauses(runtime) -> None:
    runtime.fake.script("run-1", [PromptTooLongError(), PromptTooLongError()])
    run = await runtime.orchestrator.run("run-1")
    assert run.status is AgentRunStatus.PAUSED_BUDGET
    assert runtime.fake.call_count("run-1") == 2


@pytest.mark.asyncio
async def test_transcript_failure_prevents_tool_execution(runtime) -> None:
    runtime.fake.script("run-1", [request_file_write()])
    runtime.transcript.fail_next_append = OSError("disk full")
    run = await runtime.orchestrator.run("run-1")
    assert run.status is AgentRunStatus.PAUSED_UNCERTAIN_STATE
    assert runtime.remote.write_calls == 0
```

- [ ] **Step 2: Run orchestrator tests and verify failure**

Run: `uv run pytest tests/investigation/test_orchestrator.py -q`

Expected: FAIL because the orchestrator still rebuilds `AgentTurnRequest` snapshots and does not append messages.

- [ ] **Step 3: Append the initial user message exactly once**

When a run has no transcript, append a user message containing the investigation symptom or delegated task prompt plus the fixed scope/budget attachment. A resumed run must never append a duplicate initial message.

- [ ] **Step 4: Append assistant actions before executing them**

Convert every validated `AgentTurnResult` into one assistant transcript message. Tool requests become `ToolUseBlock`; hypotheses, conclusions, delegation, stop signal, and textual status remain a bounded text JSON block. Persist the assistant message before executing any tool or spawning any child.

- [ ] **Step 5: Append one matching tool-result message after execution**

Change `_execute_tools()` to return both updated state and `ToolResultBlock` values. Append one user transcript message whose result IDs exactly match the preceding assistant tool-use IDs. Waiting approval is represented as a tool result with `WAITING_APPROVAL`; approval resolution appends the final result rather than changing the old entry.

- [ ] **Step 6: Continue based on tool-use content**

After validation, detect `ToolUseBlock` values in the assistant message. If present, execute them and continue. If absent, process delegation, completion, or the existing missing-evidence/no-progress rules. Do not use the upstream HTTP stream stop reason to decide continuation.

- [ ] **Step 7: Execute consecutive concurrency-safe batches**

Partition tool requests without reordering them. Consecutive `concurrency_safe=True` requests execute with `asyncio.gather`; each unsafe request forms its own serial batch. Persist every ToolCall as `RUNNING` before starting its coroutine, retain the existing post-await reload that prevents stale usage writes, and emit results in original request order. A failed or approval-blocked serial batch prevents later batches from starting.

- [ ] **Step 8: Add the one-shot reactive retry**

```python
try:
    result = await self._provider.generate_turn(request)
except PromptTooLongError:
    if self._context.reactive_attempted(run.agent_run_id, round_number):
        return self._pause_prompt_too_long(run, investigation, now)
    request = await self._context.reactive_request(run, investigation, round_number)
    result = await self._provider.generate_turn(request)
```

Do not count the failed prompt-too-long request as a completed agent round. Record both attempts in compaction state and runtime events.

- [ ] **Step 9: Handle manual compact requests**

When the assistant requests `compact_context`, call `semantic_compact(manual=True)` instead of remote execution. On success append the compact boundary and matching tool result, reset the semantic breaker, and continue from the new active context. On failure append a failed tool result and continue under deterministic compaction; do not erase the previous valid boundary.

- [ ] **Step 10: Keep child conversations isolated**

Initialize child transcript only from its `DelegatedTaskPackage`, narrowed scope, budget, authorized evidence IDs, and completion criteria. On completion, append a bounded ChildReport notification to the parent transcript; do not copy child messages. Assert parent cancellation still propagates through the existing child task tracking.

- [ ] **Step 11: Run orchestrator and live runtime tests**

Run: `uv run pytest tests/investigation/test_orchestrator.py tests/integration/test_live_agent_runtime.py -q`

Expected: PASS.

- [ ] **Step 12: Commit Task 6**

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py apps/control-plane/src/incidentlens_control_plane/investigation/provider.py tests/investigation/test_orchestrator.py tests/integration/test_live_agent_runtime.py
git commit -m "feat(agent): run persistent compactable conversations"
```

---

### Task 7: Recover Conversations and Expose Read-Only Inspection

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/recovery.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/service.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/routes/investigations.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/config.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Modify: `README.md`
- Modify: `docs/agent-memory-context-design.md`
- Modify: `tests/investigation/test_recovery.py`
- Modify: `tests/web/test_investigations_api.py`

**Interfaces:**
- Consumes: transcript, compact boundary, Todo, Session Memory, and compaction state stores.
- Produces: safe restart validation and read-only endpoints for transcript, plan, and memory.
- Produces: final runtime configuration wiring.

- [ ] **Step 1: Write failing restart and API tests**

```python
@pytest.mark.asyncio
async def test_restart_recovers_boundary_plan_and_tail_without_replaying_write(runtime_factory) -> None:
    first = runtime_factory()
    seed_completed_write_and_compact_boundary(first)
    second = runtime_factory(database=first.database)
    await second.recovery.startup()
    active = second.context.build(second.store.get_agent_run("run-1"), second.store.get_investigation("inv-1"), ())
    assert active.memory.revision == 2
    assert [item.todo_id for item in active.todos] == ["verify"]
    assert second.remote.write_calls == 0


def test_investigation_context_api(client, store) -> None:
    seed_transcript_memory_and_plan(store)
    response = client.get("/api/investigations/inv-1/context")
    assert response.status_code == 200
    assert response.json()["latest_memory"]["revision"] == 1
    assert response.json()["todos"][0]["todo_id"] == "inspect"
    assert response.json()["transcript"][0]["sequence"] == 1
```

- [ ] **Step 2: Run recovery and API tests and verify failure**

Run: `uv run pytest tests/investigation/test_recovery.py tests/web/test_investigations_api.py -q`

Expected: FAIL because recovery does not validate transcript tails and the context endpoint lacks transcript/plan data.

- [ ] **Step 3: Validate transcript state during startup**

For each non-terminal run, load the latest boundary, memory, Todo, and tail. If tool pairing is invalid, fall back to the previous valid boundary and park the run in `PAUSED_UNCERTAIN_STATE`. Keep existing handling for dangerous in-flight calls; never synthesize or replay a completed tool result from transcript alone.

- [ ] **Step 4: Replace old context settings with explicit budgets**

```python
agent_context_window_tokens: int = Field(default=128_000, ge=8_000, le=2_000_000)
agent_context_max_output_tokens: int = Field(default=8_000, ge=256, le=128_000)
agent_context_reserve_tokens: int = Field(default=13_000, ge=1_000, le=128_000)
agent_tool_result_budget_chars: int = Field(default=200_000, ge=10_000, le=5_000_000)
agent_context_max_message_groups: int = Field(default=50, ge=10, le=500)
agent_context_keep_recent_tool_results: int = Field(default=3, ge=1, le=20)
agent_compact_max_failures: int = Field(default=3, ge=1, le=10)
agent_reactive_keep_recent_groups: int = Field(default=5, ge=1, le=20)
```

Remove `agent_context_max_chars` and `agent_context_compact_every_rounds` after updating all runtime construction and tests. Keep environment aliases only if existing deployments already expose those names; otherwise fail clearly on obsolete configuration.

- [ ] **Step 5: Return bounded read-only context inspection**

Add a service method and `GET /api/investigations/{investigation_id}/context` response containing runs with latest memory, Todo, compact boundaries, and paginated transcript messages. Do not include complete EvidenceStore content in this endpoint; transcript entries already contain the model-visible preview and references.

- [ ] **Step 6: Update project documentation**

Revise `README.md` and `docs/agent-memory-context-design.md` to describe the implemented continuous message loop, Todo, transcript, compaction order, semantic breaker, reactive retry, child isolation, and recovery behavior. Mark Project Memory as out of scope.

- [ ] **Step 7: Run focused recovery, API, and configuration tests**

Run: `uv run pytest tests/investigation/test_recovery.py tests/web/test_investigations_api.py tests/test_app.py -q`

Expected: PASS.

- [ ] **Step 8: Run lint and the full test suite**

Run: `uv run ruff check apps/control-plane/src tests`

Expected: PASS with no lint errors.

Run: `uv run pytest -q`

Expected: PASS. The existing third-party Starlette/httpx deprecation warning may remain; no new warnings are accepted.

- [ ] **Step 9: Commit Task 7**

```bash
git add README.md docs/agent-memory-context-design.md apps/control-plane/src/incidentlens_control_plane/config.py apps/control-plane/src/incidentlens_control_plane/runtime.py apps/control-plane/src/incidentlens_control_plane/investigation/recovery.py apps/control-plane/src/incidentlens_control_plane/investigation/service.py apps/control-plane/src/incidentlens_control_plane/routes/investigations.py tests/investigation/test_recovery.py tests/web/test_investigations_api.py tests/test_app.py
git commit -m "feat(agent): recover and inspect compacted conversations"
```
