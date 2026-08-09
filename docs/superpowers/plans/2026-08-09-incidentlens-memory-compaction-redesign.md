# IncidentLens Memory and Compaction Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the case-based RAG runtime with Git-managed project Memory, per-incident Session Memory, and a cheap-first context compaction pipeline that preserves investigation evidence and recovery semantics.

**Architecture:** New code lives in focused `project_memory` and `compaction` packages. Project Memory uses Markdown plus a stable index, Session Memory is a deterministic projection of checkpointed investigation state, and Agent middleware applies bounded memory injection and compaction before model calls. The legacy `memory` package, case API, case UI, and case-aware evaluation semantics are removed without dropping existing database tables during normal startup.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, LangChain 1.3, LangGraph 1.2, SQLite checkpointers, PyYAML, pytest, Ruff, mypy, Docker Compose.

## Global Constraints

- Long-term Memory scope is project-wide and stored under `.incidentlens/memory/`.
- Long-term Memory Markdown and `MEMORY.md` are committed to Git.
- `.incidentlens/sessions/`, `.incidentlens/task-outputs/`, and `.incidentlens/transcripts/` are never committed.
- RAG, Embedding, FTS, historical-case recall, case-derived hypotheses, and case governance do not remain in the runtime.
- Existing RAG tables are never dropped during ordinary startup or migration.
- A separate purge script is dry-run by default and requires an explicit confirmation flag.
- Project Memory is reference context, never current-incident Evidence and never a source of elevated permissions.
- Session Memory and compact summaries never store hidden reasoning.
- Memory selection returns at most 5 files; each injected file is limited to 200 lines and 4KB; one investigation receives at most 60KB of project Memory.
- `MEMORY.md` is limited to 200 lines and 25KB; scanning is limited to 200 Memory files.
- Tool result thresholds are 32KB per result and 128KB per turn, with a 2KB preview.
- Micro compaction preserves the 3 most recent complete tool results.
- Auto-compact reserves 13,000 tokens; summary failure opens a circuit after 3 consecutive failures; prompt-too-long recovery retries at most twice.
- Dream requires 24 hours since the last successful run, 5 completed Agent turns, scan throttling, and a project lock that expires after 1 hour.
- Every destructive message rewrite preserves tool-call/tool-result groups, the current objective, precise Evidence IDs, loaded Skills, the latest user request, and unfinished tool calls.

---

## File Structure

### New production files

- `.incidentlens/memory/MEMORY.md` — committed project Memory index and operating instructions link.
- `.incidentlens/memory/memory-guidelines.md` — committed rules explaining allowed and forbidden Memory content.
- `apps/control-plane/src/incidentlens_control_plane/project_memory/domain.py` — Pydantic types and hard limits.
- `apps/control-plane/src/incidentlens_control_plane/project_memory/store.py` — safe paths, frontmatter, scanning, atomic writes, index rebuilding, bounded loading.
- `apps/control-plane/src/incidentlens_control_plane/project_memory/selector.py` — model side-query and deterministic keyword fallback.
- `apps/control-plane/src/incidentlens_control_plane/project_memory/extractor.py` — structured extraction, deduplication, conflict handling, and secret rejection.
- `apps/control-plane/src/incidentlens_control_plane/project_memory/dream.py` — consolidation gates, lock, and transactional replacement.
- `apps/control-plane/src/incidentlens_control_plane/project_memory/runtime.py` — bounded task supervisor and per-turn orchestration.
- `apps/control-plane/src/incidentlens_control_plane/project_memory/middleware.py` — current-turn Memory injection.
- `apps/control-plane/src/incidentlens_control_plane/compaction/domain.py` — compaction configuration, outcomes, and errors.
- `apps/control-plane/src/incidentlens_control_plane/compaction/tool_budget.py` — large output persistence.
- `apps/control-plane/src/incidentlens_control_plane/compaction/micro.py` — tool group collection, micro compaction, and middle snipping.
- `apps/control-plane/src/incidentlens_control_plane/compaction/session.py` — Session Memory projection, validation, persistence, and restore message.
- `apps/control-plane/src/incidentlens_control_plane/compaction/summary.py` — text-only summary fallback and circuit breaker.
- `apps/control-plane/src/incidentlens_control_plane/compaction/middleware.py` — fixed-order pre-model compaction and reactive retry.
- `scripts/purge_legacy_rag.py` — explicit legacy table cleanup.

### New test files

- `tests/project_memory/test_domain.py`
- `tests/project_memory/test_store.py`
- `tests/project_memory/test_selector.py`
- `tests/project_memory/test_extractor.py`
- `tests/project_memory/test_dream.py`
- `tests/project_memory/test_runtime.py`
- `tests/compaction/test_tool_budget.py`
- `tests/compaction/test_micro.py`
- `tests/compaction/test_session.py`
- `tests/compaction/test_summary.py`
- `tests/compaction/test_middleware.py`
- `tests/integration/test_memory_compaction_flow.py`
- `tests/services/test_legacy_rag_purge.py`

### Legacy files removed after callers are migrated

- `apps/control-plane/src/incidentlens_control_plane/memory/domain.py`
- `apps/control-plane/src/incidentlens_control_plane/memory/models.py`
- `apps/control-plane/src/incidentlens_control_plane/memory/repository.py`
- `apps/control-plane/src/incidentlens_control_plane/memory/service.py`
- `apps/control-plane/src/incidentlens_control_plane/memory/retrieval.py`
- `apps/control-plane/src/incidentlens_control_plane/memory/embedding.py`
- `apps/control-plane/src/incidentlens_control_plane/memory/integration.py`
- `apps/control-plane/src/incidentlens_control_plane/memory/migrations.py`
- `apps/control-plane/src/incidentlens_control_plane/routes/cases.py`
- Existing RAG-only tests under `tests/memory/`, `tests/agent/test_memory_integration.py`, `tests/web/test_case_governance_api.py`, and `tests/integration/test_memory_governance_flow.py`.

---

### Task 1: Cut the Agent off from legacy RAG

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/types.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/state.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/projection.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/prompts.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/runtime.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/baseline.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/factory.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `tests/agent/conftest.py`
- Modify: `tests/agent/test_runtime.py`
- Modify: `tests/agent/test_langgraph_state.py`
- Create: `tests/agent/test_no_rag_runtime.py`

**Interfaces:**
- Removes: `retrieved_cases`, `case_id`, and `case_status` from `IncidentAgentState` and `InvestigationState`.
- Removes: `case_repository` and `memory` parameters from `build_investigation_engine()` and engine constructors.
- Preserves: all current Evidence, conclusion-gate, checkpoint, and read-only tool behavior.

- [ ] **Step 1: Write failing tests proving the runtime has no case inputs**

```python
# tests/agent/test_no_rag_runtime.py
import inspect

from incidentlens_control_plane.agent.factory import build_investigation_engine
from incidentlens_control_plane.agent.prompts import SYSTEM_PROMPT, build_agent_context


def test_engine_factory_has_no_legacy_case_dependencies() -> None:
    parameters = inspect.signature(build_investigation_engine).parameters
    assert "case_repository" not in parameters
    assert "memory" not in parameters


def test_agent_prompt_and_context_have_no_historical_cases() -> None:
    context = build_agent_context({"incident_id": "inc-1", "alert": {}})
    combined = f"{SYSTEM_PROMPT}\n{context}".lower()
    assert "historical case" not in combined
    assert "retrieved_cases" not in combined
```

- [ ] **Step 2: Run the focused tests and observe the expected failure**

Run: `python -m pytest tests/agent/test_no_rag_runtime.py tests/agent/test_runtime.py tests/agent/test_langgraph_state.py -q`

Expected: failures show the factory still accepts `case_repository`/`memory` and prompts still contain historical-case text.

- [ ] **Step 3: Remove legacy fields and coordinator calls**

Use this final factory signature:

```python
def build_investigation_engine(
    *,
    mode: RuntimeMode,
    telemetry_repo: Any,
    toolkit: ReadOnlyToolkit,
    audit_store: InvestigationAuditStore,
    checkpointer: Any | None = None,
    skill_runtime: Any | None = None,
    model_registry: ModelRegistry | None = None,
    project_memory_runtime: Any | None = None,
    compaction_runtime: Any | None = None,
) -> InvestigationEngineProtocol:
    ...
```

The two new runtime parameters remain `None` in this task and are wired in Task 8. Remove all calls to `prepare()` and `finalize()`. Remove historical-case prompt sections and context rendering. Update state projection and fixtures so checkpointed investigations no longer carry case fields.

- [ ] **Step 4: Remove production construction of `InvestigationMemoryCoordinator`**

In `main.py`, stop constructing or injecting `CaseRepository`, `CaseService`, `HybridCaseRetriever`, and `InvestigationMemoryCoordinator` into the Agent. Do not yet remove the case router or case service needed by the legacy Web surface; Task 9 deletes that surface after new Memory is integrated.

- [ ] **Step 5: Run Agent and state tests**

Run: `python -m pytest tests/agent --ignore=tests/agent/test_memory_integration.py -q`

Expected: all non-RAG Agent tests pass. The intentionally obsolete RAG-specific module remains ignored until its deletion in Task 9.

- [ ] **Step 6: Commit the runtime cut-off**

```bash
git add apps/control-plane/src/incidentlens_control_plane/agent apps/control-plane/src/incidentlens_control_plane/main.py tests/agent
git commit -m "refactor: remove rag from agent runtime"
```

---

### Task 2: Add Memory domain types, configuration, and safe file store

**Files:**
- Create: `.incidentlens/memory/MEMORY.md`
- Create: `.incidentlens/memory/memory-guidelines.md`
- Modify: `.gitignore`
- Create: `apps/control-plane/src/incidentlens_control_plane/project_memory/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/project_memory/domain.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/project_memory/store.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/llm/config.py`
- Modify: `config/models.yaml`
- Create: `tests/project_memory/__init__.py`
- Create: `tests/project_memory/test_domain.py`
- Create: `tests/project_memory/test_store.py`
- Modify: `tests/llm/test_config.py`

**Interfaces:**
- Produces: `MemoryLimits`, `MemoryRecord`, `MemoryCandidate`, `MemoryCatalogEntry`, `MemoryWriteResult`, `LoadedMemories`.
- Produces: `ProjectMemoryStore.scan()`, `.catalog()`, `.write()`, `.load()`, `.rebuild_index()`.
- Produces model budget fields: `context_window_tokens: int` and `reserved_output_tokens: int`.

- [ ] **Step 1: Write domain and model configuration tests**

```python
def test_memory_candidate_accepts_only_project_types() -> None:
    candidate = MemoryCandidate(
        name="deployment-entry",
        description="Where deployment configuration is composed",
        type=MemoryType.REFERENCE,
        body="## What\nconfig/models.yaml",
    )
    assert candidate.name == "deployment-entry"


def test_model_profile_requires_explicit_context_budget(valid_profile: dict) -> None:
    profile = ModelProfile.model_validate({
        **valid_profile,
        "context_window_tokens": 128_000,
        "reserved_output_tokens": 8_000,
    })
    assert profile.context_window_tokens - profile.reserved_output_tokens == 120_000
```

- [ ] **Step 2: Write store tests for frontmatter, limits, and atomic behavior**

```python
def test_write_rebuilds_bounded_index(tmp_path: Path) -> None:
    store = ProjectMemoryStore(tmp_path / ".incidentlens" / "memory")
    result = store.write(MemoryCandidate(
        name="deployment-entry",
        description="Deployment configuration entry",
        type=MemoryType.REFERENCE,
        body="## What\nconfig/models.yaml",
    ))
    assert result.action == "created"
    assert "deployment-entry.md" in (store.root / "MEMORY.md").read_text()


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    store = ProjectMemoryStore(tmp_path / "memory")
    outside = tmp_path / "outside"
    outside.mkdir()
    store.root.mkdir(parents=True)
    (store.root / "escape.md").symlink_to(outside / "secret.md")
    with pytest.raises(MemoryPathError):
        store.load(["escape.md"])
```

- [ ] **Step 3: Run the tests and observe missing types/store failures**

Run: `python -m pytest tests/project_memory/test_domain.py tests/project_memory/test_store.py tests/llm/test_config.py -q`

Expected: import failures for `project_memory` and validation failures for missing model budget fields.

- [ ] **Step 4: Implement exact domain contracts**

```python
class MemoryType(StrEnum):
    PROJECT = "project"
    PROCEDURE = "procedure"
    FEEDBACK = "feedback"
    REFERENCE = "reference"


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=96)
    description: str = Field(min_length=1, max_length=240)
    type: MemoryType
    body: str = Field(min_length=1, max_length=16_384)
```

Define hard defaults in an immutable `MemoryLimits` model. Reject attempts to configure values above specification maxima.

- [ ] **Step 5: Implement safe parsing and atomic writes**

`ProjectMemoryStore` must use `yaml.safe_load`, resolved-path containment checks, `os.open(..., O_NOFOLLOW)` where supported, a same-directory temporary file, `flush`, `os.fsync`, and `os.replace`. `scan()` skips invalid files and returns diagnostics instead of failing the directory. `rebuild_index()` sorts by `updated_at` descending then filename ascending and enforces both index limits.

- [ ] **Step 6: Add committed Memory guidance and gitignore rules**

The committed `memory-guidelines.md` states that project Memory is reference context, forbids secrets/current telemetry/hidden reasoning, and documents the four types. Add only these ignore patterns:

```gitignore
.incidentlens/sessions/
.incidentlens/task-outputs/
.incidentlens/transcripts/
```

- [ ] **Step 7: Run focused quality gates**

Run: `python -m pytest tests/project_memory/test_domain.py tests/project_memory/test_store.py tests/llm/test_config.py -q`

Run: `python -m ruff check apps/control-plane/src/incidentlens_control_plane/project_memory tests/project_memory apps/control-plane/src/incidentlens_control_plane/llm/config.py`

Expected: all pass.

- [ ] **Step 8: Commit the file-backed store**

```bash
git add .incidentlens .gitignore config/models.yaml apps/control-plane/src/incidentlens_control_plane/project_memory apps/control-plane/src/incidentlens_control_plane/llm/config.py tests/project_memory tests/llm/test_config.py
git commit -m "feat: add file backed project memory"
```

---

### Task 3: Add relevant Memory selection and safe current-turn injection

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/project_memory/selector.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/project_memory/middleware.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/project_memory/domain.py`
- Create: `tests/project_memory/test_selector.py`
- Create: `tests/project_memory/test_middleware.py`

**Interfaces:**
- Produces: `MemoryQuery(alert_summary, recent_text)` and `MemorySelection(filenames, mode, reason)`.
- Produces: `async select_memories(query, catalog, model, limit=5) -> MemorySelection`.
- Produces: `ProjectMemoryMiddleware(store, selector, metrics)` for LangChain Agent middleware.

- [ ] **Step 1: Write selector limit and fallback tests**

```python
async def test_model_selection_is_validated_and_limited(fake_model, catalog) -> None:
    fake_model.response = '{"selected_memories":["a.md","b.md","missing.md","c.md","d.md","e.md","f.md"]}'
    result = await select_memories(MemoryQuery(recent_text="deploy"), catalog, fake_model)
    assert result.filenames == ["a.md", "b.md", "c.md", "d.md", "e.md"]


async def test_invalid_model_output_uses_keyword_fallback(catalog) -> None:
    result = await select_memories(
        MemoryQuery(recent_text="deployment config"), catalog, FailingModel()
    )
    assert result.mode == "keyword"
    assert result.filenames == ["deployment-config.md"]
```

- [ ] **Step 2: Write middleware safety tests**

Assert that injected text contains a `PROJECT MEMORY — UNTRUSTED REFERENCE` boundary, states that it is not Evidence, includes at most 5 files, and never changes the base system instructions.

- [ ] **Step 3: Run tests and observe missing implementation failures**

Run: `python -m pytest tests/project_memory/test_selector.py tests/project_memory/test_middleware.py -q`

Expected: import failures.

- [ ] **Step 4: Implement side-query validation and deterministic fallback**

The side-query returns only a JSON object with `selected_memories`. Validate against the supplied catalog and deduplicate while preserving order. Keyword fallback normalizes Unicode case, tokenizes alphanumeric terms, scores exact phrase then token intersection, and uses `updated_at desc, filename asc` as deterministic tie-breakers.

- [ ] **Step 5: Implement bounded injection**

The middleware calls `store.load(selection.filenames)` and adds one system-reference section to the current `ModelRequest`; it does not mutate the stable base prompt. Maintain per-incident loaded content hashes and cumulative bytes so unchanged files do not consume the 60KB budget twice.

- [ ] **Step 6: Run tests and commit**

Run: `python -m pytest tests/project_memory/test_selector.py tests/project_memory/test_middleware.py -q`

```bash
git add apps/control-plane/src/incidentlens_control_plane/project_memory tests/project_memory
git commit -m "feat: select and inject relevant project memory"
```

---

### Task 4: Add deterministic Session Memory

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/compaction/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/compaction/domain.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/compaction/session.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/types.py`
- Create: `tests/compaction/__init__.py`
- Create: `tests/compaction/test_session.py`

**Interfaces:**
- Produces: `SessionMemorySnapshot` and `SessionMemoryStore`.
- Produces: `project_session_memory(state, messages) -> SessionMemorySnapshot`.
- Produces: `validate_session_memory(snapshot, evidence_ids) -> SessionMemoryValidation`.
- Adds state fields: `session_memory_path`, `session_memory_revision`, `last_compaction_id`, `pre_compact_transcript_path`.

- [ ] **Step 1: Write projection and completeness tests**

```python
def test_projection_preserves_exact_evidence_ids(tmp_path: Path, investigation_state) -> None:
    snapshot = project_session_memory(investigation_state, [])
    text = snapshot.to_markdown()
    assert "ev-12" in text
    assert "downstream-timeout" in text
    assert "## Next action" in text


def test_missing_evidence_reference_disables_fast_compaction(investigation_state) -> None:
    snapshot = project_session_memory(investigation_state, [])
    snapshot.verified_facts = []
    validation = validate_session_memory(snapshot, {"ev-12"})
    assert not validation.complete
    assert validation.reason == "missing_evidence_ids"
```

- [ ] **Step 2: Run tests and observe failures**

Run: `python -m pytest tests/compaction/test_session.py -q`

- [ ] **Step 3: Implement deterministic projection**

Project Objective, verified facts, rejected directions, loaded Skills, completed work, next action, constraints, output references, budget, and recoverable errors from structured state. Never call a model. Bound every section and preserve Evidence IDs verbatim.

- [ ] **Step 4: Implement atomic per-incident persistence**

`SessionMemoryStore.write(incident_id, snapshot)` writes `.incidentlens/sessions/<safe-incident-id>/memory.md` atomically and increments a revision in frontmatter. It rejects unsafe incident IDs and symlink escapes.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest tests/compaction/test_session.py tests/agent/test_langgraph_state.py -q`

```bash
git add apps/control-plane/src/incidentlens_control_plane/compaction apps/control-plane/src/incidentlens_control_plane/agent/types.py tests/compaction
git commit -m "feat: persist deterministic session memory"
```

---

### Task 5: Implement zero-model compaction layers

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/compaction/tool_budget.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/compaction/micro.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/compaction/domain.py`
- Create: `tests/compaction/test_tool_budget.py`
- Create: `tests/compaction/test_micro.py`

**Interfaces:**
- Produces: `persist_oversized_tool_results(messages, incident_id, store, limits) -> CompactionResult`.
- Produces: `micro_compact(messages, keep_recent=3) -> CompactionResult`.
- Produces: `snip_middle(messages, target_tokens) -> CompactionResult`.
- Produces: `MessageGroup` collector that never splits calls from results.

- [ ] **Step 1: Write exact threshold and hash tests**

```python
def test_single_large_result_is_persisted(tmp_path: Path) -> None:
    result = persist_oversized_tool_results(
        messages_with_tool_result("x" * 32_769),
        incident_id="inc-1",
        store=ToolOutputStore(tmp_path),
        limits=CompactionLimits(),
    )
    reference = result.messages[-1].content[0]
    persisted = (tmp_path / reference["path"]).read_bytes()
    assert hashlib.sha256(persisted).hexdigest() == reference["sha256"]
    assert len(reference["preview"].encode()) <= 2_048
```

- [ ] **Step 2: Write group integrity and recent-result tests**

```python
def test_micro_compact_keeps_three_recent_complete_results() -> None:
    result = micro_compact(six_complete_tool_groups(), keep_recent=3)
    assert count_full_tool_results(result.messages) == 3


def test_snip_never_splits_tool_group() -> None:
    result = snip_middle(conversation_with_tool_groups(), target_tokens=400)
    assert all_tool_calls_have_results(result.messages)
```

- [ ] **Step 3: Run tests and observe failures**

Run: `python -m pytest tests/compaction/test_tool_budget.py tests/compaction/test_micro.py -q`

- [ ] **Step 4: Implement `ToolOutputStore` and threshold processing**

Persist the largest current-turn results first until under 128KB. Write bytes atomically, calculate SHA-256 from persisted bytes, use safe incident/tool IDs, and return a structured reference containing path, size, digest, preview, and reread instruction. If persistence fails, leave the original ToolMessage unchanged.

- [ ] **Step 5: Implement message grouping, micro compact, and snip**

Group `AIMessage.tool_calls` with all matching `ToolMessage.tool_call_id` values. Replace only completed old result payloads. Snip complete middle groups, preserve initial objective and recent groups, and insert one bounded marker with removed group/message counts.

- [ ] **Step 6: Run focused tests and commit**

Run: `python -m pytest tests/compaction/test_tool_budget.py tests/compaction/test_micro.py -q`

```bash
git add apps/control-plane/src/incidentlens_control_plane/compaction tests/compaction
git commit -m "feat: add zero model context compaction"
```

---

### Task 6: Add Session fast compact, summary fallback, and reactive recovery

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/compaction/summary.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/compaction/middleware.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/compaction/domain.py`
- Create: `tests/compaction/test_summary.py`
- Create: `tests/compaction/test_middleware.py`

**Interfaces:**
- Produces: `SummaryCircuitBreaker(max_failures=3)`.
- Produces: `async summarize_history(messages, session_memory, model) -> SummaryResult`.
- Produces: `CompactionMiddleware(runtime, limits)`.
- Emits: `ERROR_CONTEXT_TOO_LARGE = "context_too_large"` after two failed reactive retries.

- [ ] **Step 1: Write fast-path and summary circuit tests**

```python
async def test_complete_session_memory_skips_summary_model(compaction_harness) -> None:
    result = await compaction_harness.compact(oversized_messages(), complete_session=True)
    assert result.mode == "session_memory"
    assert compaction_harness.summary_model.calls == 0


async def test_three_summary_failures_open_circuit() -> None:
    breaker = SummaryCircuitBreaker(max_failures=3)
    for _ in range(3):
        breaker.record_failure()
    assert not breaker.allow_request()
```

- [ ] **Step 2: Write prompt-too-long recovery tests**

Test a handler that raises a provider-shaped prompt-too-long error three times. Assert exactly two retries, preservation of objective/Evidence/latest user message, and a final recoverable `context_too_large` state update rather than an empty restart.

- [ ] **Step 3: Run tests and observe failures**

Run: `python -m pytest tests/compaction/test_summary.py tests/compaction/test_middleware.py -q`

- [ ] **Step 4: Implement fixed-order orchestration**

Before a destructive rewrite, persist a JSONL transcript and update Session Memory. Apply tool budget, middle snip, and micro compact. Compute the trigger as:

```python
threshold = model.context_window_tokens - model.reserved_output_tokens - 13_000
```

If still over threshold, validate Session Memory. Replace checkpoint messages using LangGraph removal semantics (`RemoveMessage(id=REMOVE_ALL_MESSAGES)` followed by restored messages), not a request-only view, so checkpoint growth is actually bounded.

- [ ] **Step 5: Implement text-only summary fallback**

Use a prompt that repeats `TEXT ONLY; DO NOT CALL TOOLS` at the beginning and end. Validate the returned summary contains the current objective and every active Evidence ID. Reject incomplete summaries and record a circuit failure.

- [ ] **Step 6: Implement reactive recovery**

Normalize provider errors through a pure `is_prompt_too_long(exc)` helper. Retry with progressively smaller complete-message budgets twice. Save checkpoint-compatible state and Session Memory before retry. Return the recoverable error code after exhaustion.

- [ ] **Step 7: Run tests and commit**

Run: `python -m pytest tests/compaction -q`

```bash
git add apps/control-plane/src/incidentlens_control_plane/compaction tests/compaction
git commit -m "feat: add recoverable context compaction"
```

---

### Task 7: Add stop-hook extraction, bounded tasks, and Dream

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/project_memory/extractor.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/project_memory/dream.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/project_memory/runtime.py`
- Create: `tests/project_memory/test_extractor.py`
- Create: `tests/project_memory/test_dream.py`
- Create: `tests/project_memory/test_runtime.py`

**Interfaces:**
- Produces: `async extract_memories(transcript_path, catalog, model) -> list[MemoryCandidate]`.
- Produces: `DreamGate.evaluate(now, turn_count, lock) -> DreamDecision`.
- Produces: `MemoryTaskSupervisor.start()`, `.submit()`, `.close(timeout_seconds)`.
- Produces: `ProjectMemoryRuntime.on_turn_start()` and `.on_turn_stop()`.

- [ ] **Step 1: Write extractor dedupe and secret rejection tests**

```python
async def test_duplicate_candidate_does_not_grow_store(runtime, existing_memory) -> None:
    result = await runtime.extract_from(transcript_with_same_fact(existing_memory))
    assert result.created == 0
    assert len(runtime.store.scan().records) == 1


async def test_candidate_with_secret_is_rejected(runtime) -> None:
    result = await runtime.extract_from(transcript_with_text("Authorization: Bearer abc123"))
    assert result.secret_rejected == 1
    assert runtime.store.scan().records == []
```

- [ ] **Step 2: Write Dream gate and lock tests**

Cover each skip reason separately: interval, scan throttle, fewer than 5 turns, valid lock. Cover a lock older than one hour and concurrent acquisition with exactly one winner.

- [ ] **Step 3: Write supervisor non-blocking and shutdown tests**

Assert `on_turn_stop()` returns before a blocked extractor finishes, duplicate turn IDs enqueue once, Dream is dropped before extraction when the queue is full, and `close()` waits only up to its timeout.

- [ ] **Step 4: Run tests and observe failures**

Run: `python -m pytest tests/project_memory/test_extractor.py tests/project_memory/test_dream.py tests/project_memory/test_runtime.py -q`

- [ ] **Step 5: Implement structured extraction**

Read only the bounded pre-compact transcript file and catalog. Require a JSON array of exact candidate fields. Apply secret scanning before semantic dedupe. Existing same-name equivalent content updates `updated_at`; conflicts receive a stable `-conflict-<content-hash-prefix>` suffix.

- [ ] **Step 6: Implement Dream transaction**

Acquire `.consolidate-lock` with exclusive creation, validate all proposed consolidated records in memory, write a staging directory, fsync it, and replace individual target files only after the complete proposal validates. On failure, leave original files and index unchanged. Store successful turn count and timestamp in the lock metadata.

- [ ] **Step 7: Implement bounded task supervision**

Use an `asyncio.Queue` with explicit task priority and named worker tasks. Track tasks in a set, consume exceptions, expose counters, and make stop submission idempotent by `(incident_id, turn_id)`.

- [ ] **Step 8: Run tests and commit**

Run: `python -m pytest tests/project_memory -q`

```bash
git add apps/control-plane/src/incidentlens_control_plane/project_memory tests/project_memory
git commit -m "feat: extract and consolidate project memory"
```

---

### Task 8: Wire Memory and compaction into Agent lifecycle

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/graph.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/runtime.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/factory.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/prompts.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/llm/registry.py`
- Modify: `infra/compose/compose.yaml`
- Modify: `tests/agent/conftest.py`
- Modify: `tests/agent/test_llm_graph.py`
- Modify: `tests/agent/test_recovery.py`
- Create: `tests/agent/test_memory_compaction_integration.py`

**Interfaces:**
- `build_investigation_agent(..., project_memory_runtime, compaction_runtime)` installs middleware in deterministic order.
- `ModelIdentity` exposes `context_window_tokens` and `reserved_output_tokens` without secrets.
- FastAPI lifespan starts and closes `MemoryTaskSupervisor`.

- [ ] **Step 1: Write middleware-order and lifecycle tests**

Assert the order is project filesystem/Skill middleware, project Memory injection, investigation context, audit/evidence/conclusion gates, compaction, report gate. Assert FastAPI shutdown calls the Memory runtime close method exactly once.

- [ ] **Step 2: Write cross-compact recovery test**

Create a real SQLite checkpointer and scripted model. Force oversized tool output, compact, interrupt, and resume. Assert the successful tool is not repeated, the Evidence ID is unchanged, the loaded Skill remains available, and the next action continues.

- [ ] **Step 3: Run tests and observe missing wiring failures**

Run: `python -m pytest tests/agent/test_memory_compaction_integration.py tests/agent/test_recovery.py -q`

- [ ] **Step 4: Wire runtime objects in `main.py`**

Resolve paths from explicit environment variables with these defaults:

```text
INCIDENTLENS_MEMORY_DIR=/app/.incidentlens/memory
INCIDENTLENS_SESSION_DIR=/data/incidentlens/sessions
INCIDENTLENS_TASK_OUTPUT_DIR=/data/incidentlens/task-outputs
INCIDENTLENS_TRANSCRIPT_DIR=/data/incidentlens/transcripts
```

For local non-Compose execution, default all paths beneath the repository `.incidentlens/`. Mount repository Memory read-write at `/app/.incidentlens/memory` and keep session/output/transcript data on the named data volume.

- [ ] **Step 5: Wire turn start, compact, and stop**

Start Memory prefetch before the model request, collect it with a bounded timeout, inject selected files, compact checkpoint messages before model calls, and submit extraction only after a normal Agent stop with no unfinished tool call. Persist or reuse the pre-compact transcript path.

- [ ] **Step 6: Add safe metrics and audit records**

Record counts, bytes, durations, modes, hashes, and skip reasons. Never record Memory bodies, user text, raw tool output, or credentials.

- [ ] **Step 7: Run Agent tests and commit**

Run: `python -m pytest tests/agent tests/project_memory tests/compaction --ignore=tests/agent/test_memory_integration.py -q`

```bash
git add apps/control-plane/src/incidentlens_control_plane/agent apps/control-plane/src/incidentlens_control_plane/main.py apps/control-plane/src/incidentlens_control_plane/llm infra/compose/compose.yaml tests/agent
git commit -m "feat: wire memory and compaction runtime"
```

---

### Task 9: Retire legacy RAG product code without dropping old data

**Files:**
- Delete: `apps/control-plane/src/incidentlens_control_plane/memory/`
- Delete: `apps/control-plane/src/incidentlens_control_plane/routes/cases.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/routes/investigations.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/services/investigation_export.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/services/demo_reset.py`
- Modify: `tests/web/conftest.py`
- Modify: `tests/web/test_investigation_export.py`
- Modify: `tests/agent/test_investigation_engine.py`
- Delete: `tests/web/test_case_governance_api.py`
- Delete: `tests/agent/test_memory_integration.py`
- Delete: all legacy RAG tests under `tests/memory/`
- Create: `scripts/purge_legacy_rag.py`
- Create: `tests/services/test_legacy_rag_purge.py`
- Create: `tests/web/test_rag_routes_retired.py`

**Interfaces:**
- `InvestigationExportService(engine, audit_store)` no longer accepts `case_service`.
- Investigation response no longer exposes `case_id` or `case_status`.
- `/api/cases*` routes are absent.
- Normal startup never creates or drops legacy tables.
- Purge CLI: `python scripts/purge_legacy_rag.py DATABASE [--confirm-drop-legacy-rag]`.

- [ ] **Step 1: Write route retirement and export tests**

```python
async def test_case_routes_are_not_registered(agent_api_client) -> None:
    response = await agent_api_client.get("/api/cases/search", params={"q": "timeout"})
    assert response.status_code == 404


async def test_export_contains_no_case_payload(export_client) -> None:
    body = (await export_client.get("/api/investigations/inc-api/export")).json()
    assert "case" not in body
    assert "case_usage" not in body
```

- [ ] **Step 2: Write non-destructive startup and purge tests**

Create a temporary SQLite database with `case_memory`, `case_fts`, and a non-RAG `investigation_audits` table. Assert ordinary app resource construction leaves all three untouched. Assert purge dry-run leaves all three untouched. Assert confirmed purge drops only the known RAG tables.

- [ ] **Step 3: Run tests and observe current RAG surface failures**

Run: `python -m pytest tests/web/test_rag_routes_retired.py tests/web/test_investigation_export.py tests/services/test_legacy_rag_purge.py -q`

- [ ] **Step 4: Delete RAG package and callers**

Remove the case router registration and overrides, case fields from API responses, case service from exports, and RAG table clearing from demo reset. Keep evaluation run tables in full reset; they are not RAG tables.

- [ ] **Step 5: Implement an allowlisted purge script**

Use this immutable allowlist:

```python
LEGACY_RAG_TABLES = (
    "case_feedback",
    "case_usage_events",
    "case_review_actions",
    "case_embeddings",
    "case_fts",
    "case_memory",
    "incidentlens_schema_versions",
)
```

Before deletion, validate that the argument is an existing regular SQLite file, print discovered allowlisted tables, and exit successfully without mutation unless `--confirm-drop-legacy-rag` is present. Execute confirmed drops in one transaction. Never interpolate discovered arbitrary names; only interpolate constants from the allowlist.

- [ ] **Step 6: Remove obsolete tests and fix shared fixtures**

Delete tests whose required behavior is intentionally removed. Replace case-aware fixtures with engine-only/export-only fixtures. Run static search to ensure no source import from `incidentlens_control_plane.memory` remains.

- [ ] **Step 7: Run focused tests and commit**

Run: `python -m pytest tests/web tests/services tests/agent -q`

Run: `rg -n "HybridCaseRetriever|InvestigationMemoryCoordinator|CaseRepository|CaseService|retrieved_cases|case_status" apps scripts`

Expected: no runtime matches.

```bash
git add -A apps/control-plane/src tests scripts
git commit -m "refactor: retire legacy rag product surface"
```

---

### Task 10: Remove RAG evaluation/UI semantics and update product documentation

**Files:**
- Modify: `packages/evaluation/src/incidentlens_evaluation/runner.py`
- Modify: `packages/evaluation/src/incidentlens_evaluation/metrics.py`
- Modify: `packages/evaluation/src/incidentlens_evaluation/cli.py`
- Modify: `tests/evaluation/test_metrics.py`
- Modify: `tests/evaluation/test_run_store.py`
- Modify: `apps/control-plane/static/index.html`
- Modify: `apps/control-plane/static/assets/index-vGry3IAM.js`
- Modify: `apps/control-plane/static/assets/index-Zu2hdl1G.css`
- Modify: `tests/web/test_dashboard_contract.py`
- Modify: `README.md`
- Modify: `REQUIREMENTS.md`
- Modify: `docs/evaluation.md`
- Create: `docs/memory-and-compaction.md`
- Modify: `tests/test_test_topology.py`

**Interfaces:**
- Evaluation strategy names become `deterministic_baseline` and `llm_agent`; neither seeds or queries historical cases.
- Removes `historical_cases_adopted`, `historical_cases_misleading`, and `historical_case_misleading_rate`.
- Adds observable result fields `project_memories_loaded`, `compaction_count`, and `summary_fallback_count`, derived from audit events.

- [ ] **Step 1: Rewrite evaluation tests around runtime strategies**

```python
def test_evaluation_strategies_do_not_include_rag() -> None:
    assert set(EVALUATION_STRATEGIES) == {"deterministic_baseline", "llm_agent"}


def test_metrics_aggregate_memory_and_compaction_observability() -> None:
    result = compute_metrics([
        RunRecord(
            root_service_expected="payment-service",
            root_service_actual="payment-service",
            evidence_reference_correct=True,
            project_memories_loaded=2,
            compaction_count=1,
            summary_fallback_count=0,
        )
    ])
    assert result.project_memories_loaded == 2
    assert result.compaction_count == 1
```

- [ ] **Step 2: Rewrite dashboard contract tests**

Assert the dashboard has no case queue/editor/search/history/feedback regions or `/api/cases` strings. Preserve investigation timeline, Evidence, export, scenarios, evaluation results, accessibility, and the hidden-reasoning prohibition. Add a compact status region showing counts and modes, not Memory contents.

- [ ] **Step 3: Run tests and observe old semantics failures**

Run: `python -m pytest tests/evaluation tests/web/test_dashboard_contract.py -q`

- [ ] **Step 4: Simplify evaluation runner and metrics**

Remove `CaseRepository`, `CaseRow`, historical seeding, and usage-event queries. Select engine mode from the two runtime strategy names. Derive Memory/compact counters from safe audit events when present and default to zero when a runtime does not emit them.

- [ ] **Step 5: Remove the case governance dashboard**

Edit the packaged static assets to remove all case API requests, governance controls, case scoring labels, and case state. Keep the existing visual language and dashboard navigation. Do not introduce a frontend build dependency that the repository does not currently contain.

- [ ] **Step 6: Update requirements and operator documentation**

Replace FR-06/FR-07 with project Memory, Session Memory, compaction, safe loading, stop extraction, Dream, and non-destructive RAG retirement requirements. Document file formats, Git policy, limits, Compose paths, metrics, failure modes, and the explicit purge command. Remove every claim that IncidentLens still provides RAG.

- [ ] **Step 7: Run static content checks and focused tests**

Run: `rg -n "RAG|Embedding|HybridCaseRetriever|case memory|historical case|memory_unverified|react_no_memory" README.md REQUIREMENTS.md docs packages/evaluation apps/control-plane/static`

Expected: matches appear only in historical design documents that are intentionally immutable and in the new migration explanation identifying retired behavior.

Run: `python -m pytest tests/evaluation tests/web/test_dashboard_contract.py -q`

- [ ] **Step 8: Commit product cleanup**

```bash
git add packages/evaluation tests/evaluation apps/control-plane/static tests/web/test_dashboard_contract.py README.md REQUIREMENTS.md docs tests/test_test_topology.py
git commit -m "docs: replace rag product semantics with memory"
```

---

### Task 11: Add end-to-end Memory/compact acceptance and run all gates

**Files:**
- Create: `tests/integration/test_memory_compaction_flow.py`
- Delete: `tests/integration/test_memory_governance_flow.py`
- Modify: `tests/integration/conftest.py`
- Modify: `tests/test_test_topology.py`
- Modify: `docs/phase-5-live-verification.md`

**Interfaces:**
- Acceptance proves project Memory persists across turns and process restart.
- Acceptance proves compact/resume preserves Evidence and avoids duplicate successful tools.
- Acceptance proves legacy RAG tables are untouched and unused.

- [ ] **Step 1: Write the integration acceptance test**

The test must perform this sequence:

```python
async def test_memory_compaction_survives_restart(compose_urls, project_memory_dir):
    # 1. Start an investigation that discovers a stable project procedure.
    # 2. Complete a normal Agent turn and wait for the bounded extraction hook.
    # 3. Assert one Markdown Memory and a valid MEMORY.md index exist.
    # 4. Start a related investigation and assert the file is selected.
    # 5. Force oversized tool output and verify a persisted output reference.
    # 6. Restart the control plane and resume the same incident.
    # 7. Assert Evidence IDs and loaded Skill names are unchanged.
    # 8. Assert no successful tool call is repeated.
    # 9. Assert no query touches legacy RAG tables.
```

Implement each assertion through public API, exported safe audit events, and mounted filesystem state; do not inspect model hidden reasoning.

- [ ] **Step 2: Replace the topology reference**

Update `tests/test_test_topology.py` so the new integration module is required to declare `pytestmark = pytest.mark.integration` and the removed governance module is absent.

- [ ] **Step 3: Run all non-network tests**

Run: `python -m pytest -m 'not integration and not live_llm' -q`

Expected: all pass with no legacy RAG tests collected.

- [ ] **Step 4: Run lint and type checks**

Run: `python -m ruff check . --exclude .claude`

Run: `python -m mypy apps packages`

Expected: all pass.

- [ ] **Step 5: Run static retirement checks**

Run: `rg -n "from incidentlens_control_plane\.memory|HybridCaseRetriever|InvestigationMemoryCoordinator|CaseRepository|CaseService|retrieved_cases" apps packages scripts tests`

Expected: no matches.

Run: `python -m pytest tests/services/test_legacy_rag_purge.py tests/web/test_rag_routes_retired.py -q`

Expected: all pass.

- [ ] **Step 6: Run Compose build and integration acceptance**

Run: `docker compose -f infra/compose/compose.yaml build`

Run: `docker compose -f infra/compose/compose.yaml up -d`

Run: `python -m pytest tests/integration/test_memory_compaction_flow.py -m integration -vv`

Run: `docker compose -f infra/compose/compose.yaml down`

Expected: build, health checks, and acceptance test pass. The normal `down` command preserves the named data volume.

- [ ] **Step 7: Update verification evidence with actual command output**

Record exact pass counts, lint/type results, Compose image IDs, test date, and any explicitly skipped live-model check in `docs/phase-5-live-verification.md`. Do not preserve obsolete Phase 5 RAG claims.

- [ ] **Step 8: Commit final acceptance**

```bash
git add tests/integration tests/test_test_topology.py docs/phase-5-live-verification.md
git commit -m "test: verify memory and compaction redesign"
```

---

## Final Review Checklist

- [ ] Every requirement in the approved design maps to at least one task and test.
- [ ] No runtime import from the legacy `memory` package remains.
- [ ] New startup does not create legacy case, FTS, Embedding, feedback, review, or usage tables.
- [ ] Existing legacy tables survive normal startup unchanged.
- [ ] The explicit purge command is dry-run by default and allowlist-only.
- [ ] Project Memory files and index are tracked by Git; session/output/transcript data are ignored.
- [ ] Memory selection, injection, extraction, and Dream obey all hard budgets.
- [ ] Compaction operates in the specified cheap-first order.
- [ ] Checkpoint messages are actually replaced, not only hidden from one model request.
- [ ] Prompt-too-long handling never restarts from empty state.
- [ ] Evidence IDs, loaded Skills, user constraints, and next action survive compact/resume.
- [ ] Documentation, UI, API, evaluation, and demo semantics no longer advertise RAG.
- [ ] Unit tests, non-network suite, lint, mypy, Compose build, and integration acceptance pass.
