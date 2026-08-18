# Runtime Context Compactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the existing semantic compaction contract to the configured XFYUN MaaS runtime and prove overflow recovery and failure behavior work outside test-only wiring.

**Architecture:** Add one tool-free MaaS compactor adapter beside the existing investigation provider, inject it only in `llm_agent` mode, and move the existing breaker/tail literals into `ContextBudgetPolicy`. Preserve the current transcript, memory, validation, and one-shot retry mechanisms.

**Tech Stack:** Python 3.12, asyncio, urllib OpenAI-compatible MaaS API, Pydantic, SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-08-18-runtime-context-compactor-design.md`

## Global Constraints

- Do not change transcript grouping, Session Memory fields, Todo restoration, evidence ownership, or compact-boundary persistence.
- Compactor requests contain no executable tools; use `tools: []` only if the MaaS endpoint requires the field.
- Semantic compaction runs only for explicit `compact_context` or provider-reported context overflow.
- Never overwrite the previous valid memory or boundary after a failed compaction.
- A provider overflow gets at most one reactive compact and one retry per round.
- Fake mode must remain offline and deterministic.

---

### Task 1: Make compaction limits part of the context policy

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/context.py:147`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py:173`
- Test: `tests/investigation/test_context.py`
- Test: `tests/investigation/test_compactor.py`

**Interfaces:**
- Consumes: existing `RuntimeSettings.agent_compact_max_failures` and `agent_reactive_keep_recent_groups`.
- Produces: `ContextBudgetPolicy.compact_max_failures: int` and `reactive_keep_recent_groups: int`; `AgentContextManager.reactive_request()` consumes the policy without a caller-supplied literal.

- [ ] **Step 1: Write failing policy and behavior tests**

```python
def test_context_policy_rejects_invalid_compaction_limits() -> None:
    with pytest.raises(ValueError, match="compact_max_failures"):
        ContextBudgetPolicy(compact_max_failures=0)
    with pytest.raises(ValueError, match="reactive_keep_recent_groups"):
        ContextBudgetPolicy(reactive_keep_recent_groups=0)


@pytest.mark.asyncio
async def test_configured_breaker_threshold_is_enforced(store) -> None:
    manager_ = AgentContextManager(
        store,
        compactor=FailingCompactor(),
        policy=ContextBudgetPolicy(compact_max_failures=1),
    )
    with pytest.raises(CompactionRejected):
        await manager_.semantic_compact(run_with("ev-1"))
    with pytest.raises(CompactionCircuitOpen):
        await manager_.semantic_compact(run_with("ev-1"))
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `uv run pytest tests/investigation/test_context.py tests/investigation/test_compactor.py -q`

Expected: FAIL because the policy fields do not exist and the breaker still uses the literal `3`.

- [ ] **Step 3: Add the policy fields and replace literals**

```python
@dataclass(frozen=True, slots=True)
class ContextBudgetPolicy:
    # existing fields remain unchanged
    compact_max_failures: int = 3
    reactive_keep_recent_groups: int = 5

    def __post_init__(self) -> None:
        # existing validation remains
        if self.compact_max_failures < 1:
            raise ValueError("compact_max_failures must be >= 1")
        if self.reactive_keep_recent_groups < 1:
            raise ValueError("reactive_keep_recent_groups must be >= 1")
```

Change the breaker comparison to `self._policy.compact_max_failures`. Keep
`reactive_compact(keep_recent_groups: int | None = None)` backward compatible,
but resolve `None` to `self._policy.reactive_keep_recent_groups`; make
`reactive_request()` call it without a literal.

```python
async def reactive_compact(
    self,
    run: AgentRun,
    *,
    keep_recent_groups: int | None = None,
) -> ActiveContext:
    keep_recent = (
        keep_recent_groups
        if keep_recent_groups is not None
        else self._policy.reactive_keep_recent_groups
    )
```

- [ ] **Step 4: Wire existing settings into `ContextBudgetPolicy`**

```python
policy=ContextBudgetPolicy(
    context_window=settings.agent_context_window_tokens,
    max_output_tokens=settings.agent_context_max_output_tokens,
    reserve_tokens=settings.agent_context_reserve_tokens,
    tool_result_budget_chars=settings.agent_tool_result_budget_chars,
    max_message_groups=settings.agent_context_max_message_groups,
    keep_recent_tool_results=settings.agent_context_keep_recent_tool_results,
    compact_max_failures=settings.agent_compact_max_failures,
    reactive_keep_recent_groups=settings.agent_reactive_keep_recent_groups,
)
```

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/investigation/test_context.py tests/investigation/test_compactor.py -q`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation/context.py apps/control-plane/src/incidentlens_control_plane/runtime.py tests/investigation/test_context.py tests/investigation/test_compactor.py
git commit -m "fix(agent): wire context compaction limits"
```

### Task 2: Implement the tool-free MaaS compactor adapter

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/xfyun_compactor.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/compactor.py:35`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/context.py:752`
- Test: `tests/investigation/test_xfyun_compactor.py`
- Modify: `tests/investigation/test_compactor.py`

**Interfaces:**
- Consumes: `XfyunMaaSConfig`, `CompactionRequest`, `SessionMemory`, and `ContextCompactor`.
- Produces: `XfyunMaaSCompactor(config: XfyunMaaSConfig)` with `async compact(request: CompactionRequest) -> SessionMemory`.

- [ ] **Step 1: Write adapter contract tests with a mocked HTTP boundary**

```python
@pytest.mark.asyncio
async def test_compactor_sends_no_executable_tools(config, request, memory_payload) -> None:
    compactor = XfyunMaaSCompactor(config)
    with patch.object(compactor, "_post", return_value=_response(memory_payload)) as post:
        memory = await compactor.compact(request)
    payload = post.call_args.args[0]
    assert payload.get("tools", []) == []
    assert payload["response_format"] == {"type": "json_object"}
    assert memory.agent_run_id == request.agent_run_id


@pytest.mark.asyncio
async def test_compactor_rejects_malformed_provider_shape(config, request) -> None:
    compactor = XfyunMaaSCompactor(config)
    with patch.object(compactor, "_post", return_value={"choices": []}):
        with pytest.raises(CompactionRejected, match="invalid"):
            await compactor.compact(request)
```

The fixture payload must contain all strict `SessionMemory` identity, revision,
boundary, work-state, evidence, and timestamp fields; do not let the adapter fill
missing semantic fields.

- [ ] **Step 2: Run the new tests and verify import failure**

Run: `uv run pytest tests/investigation/test_xfyun_compactor.py -q`

Expected: FAIL because `xfyun_compactor.py` does not exist.

- [ ] **Step 3: Make the compaction request self-contained**

```python
class CompactionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_run_id: str
    investigation_id: str
    through_round: int = Field(ge=0)
    through_sequence: int = Field(ge=0)
    prior_memory: SessionMemory | None = None
    messages: tuple[TranscriptMessage, ...]
    allowed_evidence_ids: tuple[str, ...] = ()
```

Populate `investigation_id=run.investigation_id` and
`through_round=run.usage.rounds` in `AgentContextManager`. Update every test
fixture constructing a `CompactionRequest`. These are read-only identity fields;
the request still has no tools or execution capability.

- [ ] **Step 4: Implement request serialization and strict response parsing**

```python
class XfyunMaaSCompactor(ContextCompactor):
    def __init__(self, config: XfyunMaaSConfig) -> None:
        self._config = config

    async def compact(self, request: CompactionRequest) -> SessionMemory:
        payload = {
            "model": self._config.model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": _compaction_messages(request),
            "tools": [],
        }
        response = await asyncio.to_thread(self._post, payload)
        try:
            content = response["choices"][0]["message"]["content"]
            return SessionMemory.model_validate_json(_strip_fence(content))
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            raise CompactionRejected("MaaS compaction response is invalid") from exc
```

Implement `_post()` with the same URL, authorization, timeout, retryable status
classification, and bounded error messages as `XfyunMaaSProvider`. Serialize
transcript blocks to bounded JSON text and instruct the model to echo the exact
`agent_run_id`, `investigation_id`, expected next revision, `through_round`, and
`through_sequence`; do not add tool schemas. The manager-side validator still
rejects any incorrect echoed identity or boundary.

- [ ] **Step 5: Add transport and redaction-focused tests**

```python
def test_compactor_maps_429_to_rejected_without_response_body(config) -> None:
    compactor = XfyunMaaSCompactor(config)
    with patch("incidentlens_control_plane.investigation.xfyun_compactor.urlopen",
               side_effect=http_error(429)):
        with pytest.raises(CompactionRejected) as excinfo:
            compactor._post({"messages": []})
    assert "secret provider body" not in str(excinfo.value)
```

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest tests/investigation/test_xfyun_compactor.py tests/investigation/test_compactor.py -q`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation/xfyun_compactor.py apps/control-plane/src/incidentlens_control_plane/investigation/compactor.py apps/control-plane/src/incidentlens_control_plane/investigation/context.py tests/investigation/test_xfyun_compactor.py tests/investigation/test_compactor.py
git commit -m "feat(agent): add MaaS context compactor"
```

### Task 3: Inject the compactor into production runtime composition

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py:156`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `XfyunMaaSCompactor` from Task 2 and existing `XfyunMaaSConfig`.
- Produces: `RuntimeServices.context_manager` with a compactor in `llm_agent` mode and no network-capable compactor in fake mode.

- [ ] **Step 1: Write runtime composition tests**

```python
def test_llm_runtime_injects_maas_compactor(tmp_path, monkeypatch) -> None:
    settings = llm_settings(tmp_path)
    runtime = build_runtime(settings, transport_factory=FakeTransportFactory())
    assert isinstance(runtime.context_manager._compactor, XfyunMaaSCompactor)


def test_fake_runtime_does_not_inject_network_compactor(tmp_path) -> None:
    runtime = build_runtime(fake_settings(tmp_path), transport_factory=FakeTransportFactory())
    assert runtime.context_manager._compactor is None
```

- [ ] **Step 2: Run the tests and verify the llm assertion fails**

Run: `uv run pytest tests/test_app.py -q`

Expected: FAIL because runtime does not instantiate `XfyunMaaSCompactor`.

- [ ] **Step 3: Create one shared MaaS config and inject the adapter**

```python
compactor = None
if settings.agent_mode == "llm_agent":
    maas_config = XfyunMaaSConfig(
        api_key=settings.xfyun_maas_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_active_model.removeprefix("xfyun-"),
    )
    provider = XfyunMaaSProvider(maas_config)
    compactor = XfyunMaaSCompactor(maas_config)

context_manager = AgentContextManager(
    investigation_store,
    policy=policy,
    compactor=compactor,
)
```

Keep the current validation requiring the API key and active model before either
adapter is constructed.

- [ ] **Step 4: Run runtime and provider tests and commit**

Run: `uv run pytest tests/test_app.py tests/investigation/test_xfyun_provider.py tests/investigation/test_xfyun_compactor.py -q`

Expected: PASS without network calls.

```bash
git add apps/control-plane/src/incidentlens_control_plane/runtime.py tests/test_app.py
git commit -m "feat(agent): enable semantic compaction in llm runtime"
```

### Task 4: Prove overflow recovery and failure preservation through the orchestrator

**Files:**
- Modify: `tests/investigation/test_orchestrator.py:1360`
- Modify: `tests/investigation/test_compactor.py:231`
- Modify: `docs/agent-memory-context-design.md`

**Interfaces:**
- Consumes: configured policy, real runtime compactor injection, existing `PromptTooLongError` handling, and `commit_compaction()`.
- Produces: regression coverage for one-shot recovery, configurable tail retention, breaker recovery, and unchanged previous boundaries.

- [ ] **Step 1: Add orchestrator tests for configured reactive behavior**

```python
@pytest.mark.asyncio
async def test_overflow_uses_configured_tail_and_retries_once(runtime) -> None:
    runtime = build_loop_runtime(
        runtime,
        policy=ContextBudgetPolicy(reactive_keep_recent_groups=2),
    )
    seed_complete_groups(runtime.store, count=6)
    runtime.fake.script("run-1", [PromptTooLongError(), completed_step("ev-1")])
    final = await runtime.orchestrator.run("run-1")
    assert final.status is AgentRunStatus.COMPLETED
    assert runtime.fake.call_count("run-1") == 2
    assert runtime.store.get_latest_compact_boundary("run-1").through_sequence > 0
```

- [ ] **Step 2: Add failure-preservation and manual-reset tests**

```python
@pytest.mark.asyncio
async def test_failed_reactive_compact_preserves_previous_boundary(store) -> None:
    previous = seed_valid_compaction(store)
    manager_ = AgentContextManager(
        store,
        compactor=FailingCompactor(),
        policy=ContextBudgetPolicy(compact_max_failures=1),
    )
    with pytest.raises(CompactionRejected):
        await manager_.reactive_compact(run_with("ev-1"))
    assert store.get_latest_compact_boundary("run-1") == previous
```

Retain the existing manual-success breaker test and update it to use
`compact_max_failures=1`, proving the value is no longer hard-coded.

- [ ] **Step 3: Run tests and correct only the observed integration gaps**

Run: `uv run pytest tests/investigation/test_compactor.py tests/investigation/test_orchestrator.py -q`

Expected: PASS after Tasks 1–3. A failure means one of the Task 1 policy values
was not passed through `reactive_request()` or `_semantic_compact_groups()`; wire
that exact value through without changing transcript or memory schemas.

- [ ] **Step 4: Update the context design documentation**

Document that `llm_agent` injects the MaaS compactor, fake mode is offline, the
breaker/tail are setting-driven, and overflow retries exactly once. Remove any
statement implying semantic compaction is merely an injectable prototype.

- [ ] **Step 5: Run the Phase A verification suite and commit**

Run: `uv run pytest tests/investigation/test_context.py tests/investigation/test_compactor.py tests/investigation/test_orchestrator.py tests/investigation/test_xfyun_provider.py tests/investigation/test_xfyun_compactor.py tests/test_app.py -q`

Run: `uv run ruff check apps/control-plane/src/incidentlens_control_plane/investigation apps/control-plane/src/incidentlens_control_plane/runtime.py tests/investigation tests/test_app.py`

Expected: all tests and lint checks PASS.

```bash
git add tests/investigation/test_orchestrator.py tests/investigation/test_compactor.py docs/agent-memory-context-design.md
git commit -m "test(agent): verify runtime context recovery"
```
