# Runtime Identity and Reacquisition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make tool calls collision-safe across runs and make context compaction direct the model to re-observe reproducible remote state.

**Architecture:** The harness assigns a globally unique internal tool-call ID while preserving the provider ID only as a run-local correlation key. Compacted successful observations carry a typed reacquisition recipe; control state and non-reproducible observations remain protected.

**Tech Stack:** Python 3.12, Pydantic 2, SQLite, pytest, existing IncidentLens orchestrator/context/evidence stack.

**Spec:** `docs/superpowers/specs/2026-08-21-hard-cloud-incident-terminal-design.md`

## Global Constraints

- A provider-generated tool ID is never a global database identity.
- SQLite and `evidence_read` are not the normal path for reproducible observations.
- Tool-use/result transcript pairs remain atomic and append-only.
- Approval, uncertain-state, changeset, recovery, Todo and child-report state are never micro-compacted.
- No migration may silently relabel an in-flight dangerous operation.

---

### Task 1: Harness-owned tool-call identity

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/types.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/store.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/tool_executor.py`
- Test: `tests/investigation/test_orchestrator.py`
- Test: `tests/investigation/test_store.py`

**Interfaces:**
- Consumes: provider `ToolRequest.tool_call_id` as `provider_tool_call_id` within one run.
- Produces: `allocate_tool_call_id(run_id: str, provider_tool_call_id: str) -> str` and persisted `ToolCall.provider_tool_call_id: str`.

- [ ] **Step 1: Add failing cross-run collision tests**

```python
async def test_same_provider_tool_id_executes_once_per_run(runtime):
    first = await run_one_tool(runtime, run_id="run-a", provider_id="tq1")
    second = await run_one_tool(runtime, run_id="run-b", provider_id="tq1")
    assert first.tool_call_id != second.tool_call_id
    assert first.provider_tool_call_id == second.provider_tool_call_id == "tq1"
    assert first.status is ToolCallStatus.SUCCEEDED
    assert second.status is ToolCallStatus.SUCCEEDED
```

- [ ] **Step 2: Run the focused tests and verify the second run is misclassified or conflicts**

Run: `uv run pytest tests/investigation/test_orchestrator.py tests/investigation/test_store.py -k 'provider_tool_id or collision' -v`

Expected: FAIL because `ToolCall` has no provider correlation field and `tool_call_id` is global.

- [ ] **Step 3: Add the internal identity and migration**

```python
def allocate_tool_call_id(run_id: str, provider_tool_call_id: str) -> str:
    digest = hashlib.sha256(f"{run_id}\0{provider_tool_call_id}".encode()).hexdigest()[:20]
    return f"tc-{digest}"
```

Persist both IDs, rewrite transcript blocks to the internal ID before append-before-act, and use the internal ID for idempotency, approval, recovery, evidence operation IDs and store lookups. Reject duplicate provider IDs only within the same provider turn.

- [ ] **Step 4: Run identity, transcript, approval and recovery tests**

Run: `uv run pytest tests/investigation/test_orchestrator.py tests/investigation/test_store.py tests/investigation/test_transcript.py tests/investigation/test_recovery.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation tests/investigation
git commit -m "fix(agent): namespace provider tool calls by run"
```

### Task 2: Typed observation reacquisition recipes

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/types.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/context.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/compactor.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/openai_compactor.py`
- Test: `tests/investigation/test_context.py`
- Test: `tests/investigation/test_compactor.py`

**Interfaces:**
- Consumes: successful `ToolUseBlock` + `ToolResultBlock` pairs.
- Produces: `ReacquisitionRecipe(tool_name, arguments, purpose, stale_summary)` embedded in Session Memory and compact stubs.

- [ ] **Step 1: Write failing compaction tests**

```python
def test_old_reproducible_log_result_becomes_reacquisition_recipe():
    compacted = micro_compact((log_query_group("tq1"),), keep_recent=0)
    text = compacted[0].messages[1].blocks[0].content
    assert "re-run remote tool" in text
    assert "log_query" in text
    assert "evidence_read" not in text
```

Also cover current file/config reads as reproducible and pre-change snapshots, failed tools, approvals and uncertain results as protected.

- [ ] **Step 2: Verify current evidence-reload wording fails**

Run: `uv run pytest tests/investigation/test_context.py tests/investigation/test_compactor.py -k reacquisition -v`

Expected: FAIL because the current stub says to reload EvidenceStore IDs.

- [ ] **Step 3: Implement classification and recipe serialization**

```python
class ReacquisitionRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    purpose: str
    tool_name: str
    arguments: dict[str, Any]
    stale_summary: str | None = None
```

Use an explicit allowlist for reproducible read-only tools. Never serialize secrets or host credentials. Preserve bounded immutable evidence summaries separately.

- [ ] **Step 4: Update the compactor prompt and validator**

Require `reacquisition_recipes`, `irreversible_observations`, pending actions and safety state. Reject recipes naming unavailable or mutating tools.

- [ ] **Step 5: Run context and compactor suites**

Run: `uv run pytest tests/investigation/test_context.py tests/investigation/test_compactor.py tests/investigation/test_openai_compactor.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation tests/investigation
git commit -m "feat(context): reacquire reproducible observations after compact"
```

### Task 3: Emit model, hypothesis, compact and safety events

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/events/types.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/events.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/context.py`
- Test: `tests/investigation/test_orchestrator.py`
- Test: `tests/investigation/test_hooks.py`

**Interfaces:**
- Produces durable redacted events: `agent_round.started/completed`, `hypothesis.created`, `todo.updated`, `context.compacted`, `tool_call.proposed`, `policy.decided`.

- [ ] **Step 1: Add failing ordered-event tests**

```python
assert event_types == [
    "agent_round.started",
    "agent_round.completed",
    "tool_call.proposed",
    "policy.decided",
    "tool_call.started",
    "tool_call.completed",
]
```

Assert payloads contain redacted arguments/previews and never raw credentials or hidden reasoning.

- [ ] **Step 2: Run tests to verify missing event types**

Run: `uv run pytest tests/investigation/test_orchestrator.py tests/investigation/test_hooks.py -k event -v`

Expected: FAIL on absent event types.

- [ ] **Step 3: Implement publisher methods and call sites**

Each method appends to the existing `RuntimeEventStore` before broker delivery. `context.compacted` includes before/after budget, released categories, retained control-state categories and redacted recipe counts.

- [ ] **Step 4: Run event and full investigation tests**

Run: `uv run pytest tests/investigation tests/events -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/events apps/control-plane/src/incidentlens_control_plane/investigation tests
git commit -m "feat(events): expose auditable agent lifecycle events"
```

