# Cloud Agent Harness Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace IncidentLens's fixed-round workflow with a model-directed cloud incident loop backed by reliable compaction, durable Session Memory, automatic Project Memory, and outcome-based acceptance.

**Architecture:** Preserve the remote-operation and safety foundation. Introduce one shared OpenAI-compatible transport for every model-backed subsystem, reduce the provider prompt to stable capability and safety guidance, keep pressure-driven context management in `AgentContextManager`, and add a project-scoped local memory service that extracts only verified, evidence-backed outcomes. Cloud and context-pressure acceptance are separate real-system proofs.

**Tech Stack:** Python 3.12, Pydantic, SQLite, asyncio, urllib/certifi, pytest, FastAPI runtime services, DeepSeek OpenAI-compatible API, SSH/Docker acceptance target.

**Spec:** `docs/superpowers/specs/2026-08-23-cloud-agent-harness-refactor-design.md`

## Global Constraints

- The model owns investigation, delegation, repair, verification, and completion decisions.
- Round and tool counts are budgets only; no fixed-round behavior gates may remain.
- Cloud mutations and service interruptions retain scope checks, exact approvals, backups, hashes, verification, and rollback.
- Local transcript, compaction, Session Memory, and Project Memory never require human approval.
- Existing persisted investigation records remain readable.
- Project Memory is project-scoped, local, provenance-bearing, bounded, and contains no unverified hypothesis or secret.
- No vector database, embeddings, remote memory service, TUI rewrite, or remote-gateway rewrite.

---

## File Structure

- `investigation/model_transport.py`: the only OpenAI-compatible HTTP/TLS/error boundary.
- `investigation/openai_provider.py`: turn payloads, response normalization, and stable agent guidance only.
- `investigation/openai_compactor.py`: compaction payload and strict SessionMemory parsing through the shared transport.
- `investigation/context.py`: deterministic pressure pipeline and Session Memory materialization.
- `project_memory/types.py`: ProjectMemoryEntry and extraction/selection contracts.
- `project_memory/store.py`: SQLite migration and project-scoped CRUD/supersession.
- `project_memory/service.py`: bounded selection, context rendering, extraction validation, and consolidation.
- `project_memory/openai_adapter.py`: tool-free extraction and selection prompts through shared transport.
- `runtime.py`: construct and connect the transport and Project Memory services.
- `orchestrator.py`: notify Project Memory after verified parent completion; no procedural phase machine.
- `tests/eval/context_pressure.py`: real-trace pressure/continuity evaluator.
- `tests/eval/cloud_closed_loop.py`: safety and outcome evaluator without prescribed compaction/tool order.

### Task 1: One Model Transport for Turns and Compaction

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/model_transport.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/openai_provider.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/openai_compactor.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Create: `tests/investigation/test_model_transport.py`
- Modify: `tests/investigation/test_openai_provider.py`
- Modify: `tests/investigation/test_openai_compactor.py`

**Interfaces:**
- Produces: `OpenAICompatibleTransport(config).chat_completions(payload) -> dict[str, object]`.
- Produces: `ModelTransportError(message, retryable, category)` and preserves `PromptTooLongError`.
- Consumed by: provider, compactor, and Task 5 Project Memory adapter.

- [ ] **Step 1: Write failing TLS-sharing and error-classification tests**

```python
def test_transport_uses_certifi_context(monkeypatch, config):
    opened = {}
    def fake_urlopen(request, *, timeout, context):
        opened["context"] = context
        return FakeResponse({"choices": []})
    monkeypatch.setattr(model_transport, "urlopen", fake_urlopen)
    OpenAICompatibleTransport(config).chat_completions({"model": config.model})
    assert opened["context"].get_ca_certs()

def test_certificate_failure_is_non_retryable(monkeypatch, config):
    monkeypatch.setattr(model_transport, "urlopen", raise_cert_error)
    with pytest.raises(ModelTransportError) as exc:
        OpenAICompatibleTransport(config).chat_completions({})
    assert exc.value.category == "tls_configuration"
    assert exc.value.retryable is False
```

- [ ] **Step 2: Run the tests and verify they fail because no shared transport exists**

Run: `./.venv/bin/pytest tests/investigation/test_model_transport.py -q`

Expected: FAIL importing `model_transport`.

- [ ] **Step 3: Implement the minimal shared transport**

Implement one POST path using `ssl.create_default_context(cafile=certifi.where())`. Classify 413/context-length as `PromptTooLongError`; 408/429/5xx/timeouts as retryable; TLS verification and malformed base configuration as non-retryable. Never include API keys or response bodies in errors.

- [ ] **Step 4: Inject the transport instead of constructing HTTP in adapters**

Construct one transport in `build_runtime`. Change both adapters to accept it. Remove `_post`, `urlopen`, TLS, and HTTP-error handling from provider and compactor.

- [ ] **Step 5: Run transport and adapter suites**

Run: `./.venv/bin/pytest tests/investigation/test_model_transport.py tests/investigation/test_openai_provider.py tests/investigation/test_openai_compactor.py tests/test_app.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation/model_transport.py apps/control-plane/src/incidentlens_control_plane/investigation/openai_provider.py apps/control-plane/src/incidentlens_control_plane/investigation/openai_compactor.py apps/control-plane/src/incidentlens_control_plane/runtime.py tests/investigation/test_model_transport.py tests/investigation/test_openai_provider.py tests/investigation/test_openai_compactor.py tests/test_app.py
git commit -m "refactor(agent): share model transport across harness services"
```

### Task 2: Remove the Fixed-Round Workflow

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/openai_provider.py`
- Modify: `tests/investigation/test_openai_provider.py`
- Modify: `tests/integration/test_live_model_workflow_unit.py`

**Interfaces:**
- Preserves: `OpenAICompatibleProvider.generate_turn(request) -> AgentTurnResult`.
- Produces: a stable system prompt derived from identity, actual tools, scope, safety, Todo, and evidence requirements—not `round_number`.

- [ ] **Step 1: Replace cadence tests with invariance tests**

```python
@pytest.mark.parametrize("round_number", [1, 3, 8, 12, 50])
def test_parent_prompt_does_not_encode_round_workflow(round_number):
    prompt = _system_prompt(request_at(round_number))
    assert "本轮为" not in prompt
    assert "只能调用 file_edit" not in prompt
    assert "必须只调用 compact_context" not in prompt
    assert "受保护路径" in prompt

def test_prompt_exposes_actual_tools_not_scripted_stage():
    payload = provider_payload(request_with_tools("host_read", "file_edit"))
    assert [tool["function"]["name"] for tool in payload["tools"]] == [
        "host_read", "file_edit"
    ]
```

- [ ] **Step 2: Run provider tests and verify the existing cadence fails them**

Run: `./.venv/bin/pytest tests/investigation/test_openai_provider.py -q`

Expected: FAIL on fixed-round prompt text.

- [ ] **Step 3: Delete `_system_prompt` round/delegation/change cadence**

Retain only stable guidance: act through tools, cite owned evidence, maintain Todo for complex work, distinguish observation from mutation, request exact approval through mutation tools, verify real outcomes, and stop honestly when bounded evidence is insufficient. Child task narrowing remains in `task_prompt` and scope.

- [ ] **Step 4: Replace the scripted live-model unit assertion**

Make the unit assert that an unanticipated tool sequence can continue through the generic loop and that scope/approval policy—not provider round—decides execution.

- [ ] **Step 5: Run provider and orchestrator integration suites**

Run: `./.venv/bin/pytest tests/investigation/test_openai_provider.py tests/integration/test_live_model_workflow_unit.py tests/investigation/test_orchestrator.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation/openai_provider.py tests/investigation/test_openai_provider.py tests/integration/test_live_model_workflow_unit.py
git commit -m "refactor(agent): restore model-directed investigation loop"
```

### Task 3: Pressure-Driven Compaction and Session Continuity

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/config.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/context.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/openai_compactor.py`
- Modify: `tests/investigation/test_context.py`
- Modify: `tests/investigation/test_compactor.py`
- Modify: `tests/investigation/test_openai_compactor.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Preserves: `AgentContextManager.build(...) -> ActiveContext` and atomic memory/boundary commits.
- Produces: `async AgentContextManager.prepare(...) -> ActiveContext`, which runs
  semantic pressure compaction when needed and otherwise delegates to `build`.
- Produces: a configurable semantic pressure threshold below `max_input_tokens`.
- SessionMemory continues to carry objective, facts, hypotheses, questions, actions, recipes, safety state, evidence, constraints, Todo, and next actions.

- [ ] **Step 1: Add failing pressure and continuity tests**

```python
def test_semantic_compaction_is_not_requested_below_pressure(manager, run):
    active = manager.build(run, investigation(), tools())
    assert active.budget.input_tokens < active.budget.semantic_compact_at_tokens
    assert compactor.requests == []

async def test_pressure_compaction_preserves_work_state(manager, run):
    seed_large_transcript_with_target_hash(run, sha256="a" * 64)
    active = await manager.prepare(run, investigation(), tools())
    assert active.memory.objective == investigation().symptom
    assert "a" * 64 in " ".join(active.memory.immutable_observations)
    assert active.memory.todos
    assert active.budget.input_tokens <= active.budget.max_input_tokens
```

- [ ] **Step 2: Run context tests and verify pressure semantics are missing**

Run: `./.venv/bin/pytest tests/investigation/test_context.py tests/investigation/test_compactor.py -q`

Expected: FAIL on pressure threshold/semantic request behavior.

- [ ] **Step 3: Add an explicit pressure threshold and automatic semantic path**

Run deterministic result budgeting, group-safe snip, and micro-compaction every turn. Add `prepare` as the async orchestration entry: first materialize with `build`, invoke semantic compaction only when the estimated input crosses `context_window - max_output - reserve`, then rebuild from the committed boundary. Keep deterministic memory as a safe fallback and retain one-shot reactive compaction for actual `PromptTooLongError`. Change the orchestrator request path to await `prepare`; read-only API inspection may continue using synchronous `build`.

- [ ] **Step 4: Strengthen the compaction prompt and validator contract**

Require the existing SessionMemory fields to preserve evidence-backed hashes, pending approvals/changes in `safety_state` and `pending_actions`, latest verification in `completed_actions`/`open_questions`, and concrete `next_actions`. Do not add a new schema unless a failing test proves an existing bounded field cannot represent the state.

- [ ] **Step 5: Verify failed compaction leaves the previous boundary intact**

Add coverage for retryable transport failure, non-retryable TLS failure, breaker increment, no boundary advance, and successful later manual recovery.

- [ ] **Step 6: Run context, recovery, and configuration suites**

Run: `./.venv/bin/pytest tests/investigation/test_context.py tests/investigation/test_compactor.py tests/investigation/test_openai_compactor.py tests/investigation/test_orchestrator.py tests/investigation/test_recovery.py tests/test_config.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/config.py apps/control-plane/src/incidentlens_control_plane/investigation/context.py apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py apps/control-plane/src/incidentlens_control_plane/investigation/openai_compactor.py tests/investigation/test_context.py tests/investigation/test_compactor.py tests/investigation/test_openai_compactor.py tests/investigation/test_orchestrator.py tests/investigation/test_recovery.py tests/test_config.py
git commit -m "feat(context): compact on pressure and preserve incident state"
```

### Task 4: Add Project Memory Storage and Validation

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/project_memory/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/project_memory/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/project_memory/store.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/project_memory/service.py`
- Create: `tests/project_memory/test_store.py`
- Create: `tests/project_memory/test_service.py`

**Interfaces:**
- Produces: `ProjectMemoryEntry(memory_id, project_id, service_names, fact, kind, source_investigation_id, evidence_ids, status, created_at, last_confirmed_at)`.
- Produces: `ProjectMemoryStore.migrate/upsert/list_active/supersede`.
- Produces: `ProjectMemoryService.accept_extracted(entries, investigation, owned_evidence_ids)` and `render_relevant(project_id, symptom, services, limit=5)`.

- [ ] **Step 1: Write failing storage and validation tests**

```python
def test_memory_requires_owned_evidence(service):
    with pytest.raises(ProjectMemoryRejected, match="foreign evidence"):
        service.accept_extracted(
            [entry(evidence_ids=("ev-foreign",))],
            investigation(),
            owned_evidence_ids={"ev-owned"},
        )

def test_active_memory_is_project_scoped_and_bounded(store):
    seed_memories(store, project_ids=("p1", "p2"))
    result = store.list_active("p1", limit=5)
    assert len(result) <= 5
    assert {item.project_id for item in result} == {"p1"}
```

- [ ] **Step 2: Run tests and verify the subsystem does not exist**

Run: `./.venv/bin/pytest tests/project_memory -q`

Expected: FAIL importing `project_memory`.

- [ ] **Step 3: Implement the immutable contracts and SQLite store**

Use a dedicated `project_memories` table with `record_json`, project/status indexes, and additive migration. Preserve superseded rows and provenance. Never store raw tool output.

- [ ] **Step 4: Implement deterministic safety validation**

Reject empty provenance, foreign evidence, unverified-hypothesis kind, secret-like values, oversized fields, unrelated project identity, and active duplicates. Normalize service names and allow supersession instead of destructive overwrite.

- [ ] **Step 5: Implement bounded deterministic fallback selection**

Before model selection exists, rank active entries by exact service overlap, symptom term overlap in bounded descriptions, and recency. Return at most five entries and render provenance plus “advisory; revalidate current environment.”

- [ ] **Step 6: Run Project Memory tests**

Run: `./.venv/bin/pytest tests/project_memory -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/project_memory tests/project_memory
git commit -m "feat(memory): add project-scoped verified incident memory"
```

### Task 5: Automatic Extraction, Selection, and Agent-Loop Integration

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/project_memory/openai_adapter.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/project_memory/service.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/context.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Create: `tests/project_memory/test_openai_adapter.py`
- Create: `tests/integration/test_project_memory_loop.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Produces: `OpenAIProjectMemoryAdapter.extract(request) -> tuple[ProjectMemoryCandidate, ...]`.
- Produces: `OpenAIProjectMemoryAdapter.select(catalog, query, limit) -> tuple[str, ...]`.
- `AgentContextManager` consumes a bounded rendered Project Memory attachment.
- Parent completion schedules extraction without approval; extraction failure cannot change investigation completion.

- [ ] **Step 1: Write failing extraction and cross-investigation tests**

```python
async def test_completed_parent_extracts_verified_memory(runtime):
    complete_with_conclusion(runtime, evidence_ids=("ev-1",))
    await runtime.project_memory.drain_pending()
    entries = runtime.project_memory_store.list_active("project-1", limit=5)
    assert entries[0].evidence_ids == ("ev-1",)

async def test_fresh_investigation_receives_relevant_memory_as_advisory(runtime):
    seed_verified_project_memory(runtime)
    request = build_first_parent_request(runtime, symptom="canary database errors")
    header = request.messages[0].blocks[0].text
    assert "Project memory (advisory; revalidate)" in header
    assert "source investigation" in header
```

- [ ] **Step 2: Run tests and verify integration is absent**

Run: `./.venv/bin/pytest tests/project_memory/test_openai_adapter.py tests/integration/test_project_memory_loop.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement tool-free extraction and selection adapters**

Use the Task 1 transport with `tools: []` and strict JSON. Extraction receives only terminal conclusions, bounded Session Memory, changeset/verification summaries, and owned Evidence references. Selection receives only the bounded catalog metadata and current symptom/service scope.

- [ ] **Step 4: Attach memory without blocking the main loop**

On parent terminal completion, enqueue local extraction after status persistence. Failure emits a redacted memory event and leaves completion untouched. On request construction, select and render at most five relevant active memories in the fixed context header; fall back to deterministic selection on model/parse failure.

- [ ] **Step 5: Prove hypotheses and secrets never persist**

Add tests where the model returns an unverified hypothesis, foreign Evidence ID, API key-shaped string, and raw log block. All must be rejected while a valid verified fact in the same extraction batch is retained.

- [ ] **Step 6: Run memory, context, orchestrator, and app suites**

Run: `./.venv/bin/pytest tests/project_memory tests/integration/test_project_memory_loop.py tests/investigation/test_context.py tests/investigation/test_orchestrator.py tests/test_app.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/project_memory apps/control-plane/src/incidentlens_control_plane/investigation/context.py apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py apps/control-plane/src/incidentlens_control_plane/runtime.py tests/project_memory tests/integration/test_project_memory_loop.py tests/test_app.py
git commit -m "feat(memory): integrate automatic project memory with agent loop"
```

### Task 6: Replace Trace Choreography with Capability Evaluators

**Files:**
- Modify: `tests/eval/cloud_closed_loop.py`
- Modify: `tests/eval/test_cloud_closed_loop.py`
- Create: `tests/eval/context_pressure.py`
- Create: `tests/eval/test_context_pressure.py`
- Modify: `tests/eval/types.py`
- Modify: `docs/superpowers/specs/2026-08-21-hard-cloud-incident-terminal-design.md`

**Interfaces:**
- Cloud evaluator consumes trace + matrix and checks safety/outcomes only.
- Pressure evaluator consumes trace + context metrics + final report and checks actual compaction continuity/cost reduction.

- [ ] **Step 1: Write failing evaluator tests**

```python
def test_cloud_run_may_pass_without_compaction(tmp_path):
    result = evaluate(valid_cloud_trace(compaction=False), valid_matrix())
    assert "compaction_missing" not in result.failures
    assert result.passed

def test_pressure_run_requires_continuity_and_reduction(tmp_path):
    result = evaluate_pressure(
        trace_with_compaction(),
        metrics(before_tokens=90_000, after_tokens=18_000),
        final_state(objective=True, todos=True, evidence=True, hashes=True),
    )
    assert result.passed
```

- [ ] **Step 2: Run evaluator tests and verify choreography assumptions fail**

Run: `./.venv/bin/pytest tests/eval/test_cloud_closed_loop.py tests/eval/test_context_pressure.py -q`

Expected: FAIL because cloud evaluation mandates compaction and pressure evaluation is absent.

- [ ] **Step 3: Make cloud evaluation outcome-based**

Require owned evidence, at least two supported root-cause conclusions for this scenario, approval-before-each-mutation, zero unapproved mutations, successful verification, rollback reproduction, reapplication, and final four-cell success. Do not require SubAgent, compaction, exact round, or exact tool order.

- [ ] **Step 4: Implement the pressure evaluator**

Require actual threshold crossing, successful compact boundary, token reduction, preserved objective/constraints/Todo/Evidence/relevant hashes, post-compact progress without full-history replay, and terminal task success. Give each failure a stable name.

- [ ] **Step 5: Update the older acceptance design**

Mark its mandatory-compaction and prescribed-SubAgent trace requirements superseded by the 2026-08-23 harness spec. Keep the controlled scenario and cloud safety requirements.

- [ ] **Step 6: Run all evaluator tests**

Run: `./.venv/bin/pytest tests/eval -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/eval docs/superpowers/specs/2026-08-21-hard-cloud-incident-terminal-design.md
git commit -m "test(eval): verify agent capability instead of trace choreography"
```

### Task 7: Real Provider Pressure Proof and Tencent Cloud Closed Loop

**Files:**
- Create: `docs/context-acceptance/real-provider-pressure/manifest.json`
- Create: `docs/context-acceptance/real-provider-pressure/metrics.json`
- Create: `docs/context-acceptance/real-provider-pressure/README.md`
- Create: `docs/cloud-acceptance/hard-incident/manifest.json`
- Create: `docs/cloud-acceptance/hard-incident/final-matrix.jsonl`
- Add redacted artifacts under: `docs/assets/`
- Modify: `README.md`
- Modify: `docs/agent-memory-context-design.md`

**Interfaces:**
- Consumes the Task 6 evaluators.
- Produces two independent, redacted, reproducible acceptance records.

- [ ] **Step 1: Run the full local verification baseline**

Run: `./.venv/bin/ruff check .`

Run: `./.venv/bin/pytest -q`

Expected: all tests pass; no cloud action occurs if either command fails.

- [ ] **Step 2: Run a real-model context-pressure scenario**

Use the normal `incidentlens run` entry with a safe local/read-only registered target and enough bounded tool output to cross the real semantic threshold. Do not call `compact_context` by instruction. Capture provider model, UTC timestamps, input tokens before/after, memory revision, boundary, retained objective/Todo/Evidence/hash state, and terminal outcome.

- [ ] **Step 3: Evaluate and publish the pressure record**

Run: `./.venv/bin/python -m tests.eval.context_pressure --trace <redacted-trace> --metrics docs/context-acceptance/real-provider-pressure/metrics.json --manifest docs/context-acceptance/real-provider-pressure/manifest.json`

Expected: PASS.

- [ ] **Step 4: Re-provision and verify the Tencent fault baseline**

Run: `scripts/cloud_acceptance_target.sh provision --host incidentlens-tencent`

Run the remote request matrix with `--expected pre-repair` and verify stable/10=201, stable/500=429, canary/10=503, canary/500=503.

- [ ] **Step 5: Run the unchoreographed real cloud investigation**

Use the documented one-step `incidentlens run` command with the real provider. Approve only exact displayed cloud mutations/restarts. Let the model choose investigation, delegation, and verification. Exercise one changeset rollback in the same recorded task, reproduce the failure, reapply the correct configuration, and obtain four 201 cells.

- [ ] **Step 6: Redact, hash, evaluate, and publish**

Publish cast/text/trace/report/matrix artifacts only after removing secrets, personal paths, raw host identifiers where unnecessary, and sensitive log bodies. Record SHA-256 hashes and evaluator version in both manifests.

Run: `./.venv/bin/python -m tests.eval.cloud_closed_loop --trace <redacted-trace> --matrix docs/cloud-acceptance/hard-incident/final-matrix.jsonl`

Expected: PASS.

- [ ] **Step 7: Update README with honest claims**

Document that the cloud incident is a controlled Tencent CVM scenario, the pressure proof is separate and real-provider-backed, Project Memory is local and evidence-backed, and no result implies unrestricted autonomous production mutation.

- [ ] **Step 8: Run final verification**

Run: `./.venv/bin/ruff check . && ./.venv/bin/pytest -q`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add docs/context-acceptance docs/cloud-acceptance docs/assets README.md docs/agent-memory-context-design.md
git commit -m "docs: publish real cloud harness acceptance evidence"
```
