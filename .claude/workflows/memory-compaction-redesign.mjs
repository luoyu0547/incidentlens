export const meta = {
  name: 'incidentlens-memory-compaction-redesign',
  description: 'Execute the IncidentLens Memory and Compaction Redesign implementation plan',
  phases: [
    { title: 'Phase 1: Foundation', detail: 'Tasks 1-3: Cut RAG, add Memory domain, add selection/injection' },
    { title: 'Phase 2: Compaction', detail: 'Tasks 4-6: Session Memory, zero-model compaction, summary fallback' },
    { title: 'Phase 3: Extraction & Wiring', detail: 'Tasks 7-8: Stop-hook extraction, Dream, wire into Agent lifecycle' },
    { title: 'Phase 4: Cleanup & Verification', detail: 'Tasks 9-11: Retire legacy RAG, update docs, end-to-end acceptance' },
  ],
}

import { log, phase, agent, parallel, pipeline } from './workflow-helpers.js'

// =============================================================================
// Phase 1: Foundation (Tasks 1-3)
// =============================================================================

phase('Phase 1: Foundation')

// Task 1: Cut the Agent off from legacy RAG
log('Starting Task 1: Cut the Agent off from legacy RAG')

const task1Result = await agent(`Execute Task 1: Cut the Agent off from legacy RAG

You are working on the incidentlens project at /Users/luoyu/Documents/incidentlens.

Your task is to remove legacy RAG dependencies from the Agent runtime. Follow these steps precisely:

Step 1: Write failing tests proving the runtime has no case inputs

Create tests/agent/test_no_rag_runtime.py with the following content:

```python
import inspect

from incidentlens_control_plane.agent.factory import build_investigation_engine
from incidentlens_control_plane.agent.prompts import SYSTEM_PROMPT, build_agent_context


def test_engine_factory_has_no_legacy_case_dependencies() -> None:
    parameters = inspect.signature(build_investigation_engine).parameters
    assert "case_repository" not in parameters
    assert "memory" not in parameters


def test_agent_prompt_and_context_have_no_historical_cases() -> None:
    context = build_agent_context({"incident_id": "inc-1", "alert": {}})
    combined = f"{SYSTEM_PROMPT}\\n{context}".lower()
    assert "historical case" not in combined
    assert "retrieved_cases" not in combined
```

Step 2: Run the focused tests and observe the expected failure
Run: python -m pytest tests/agent/test_no_rag_runtime.py tests/agent/test_runtime.py tests/agent/test_langgraph_state.py -q

Step 3: Remove legacy fields and coordinator calls

Modify apps/control-plane/src/incidentlens_control_plane/agent/factory.py to use this final factory signature:

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

Remove all calls to prepare() and finalize(). Remove historical-case prompt sections and context rendering. Update state projection and fixtures so checkpointed investigations no longer carry case fields.

Step 4: Remove production construction of InvestigationMemoryCoordinator
In main.py, stop constructing or injecting CaseRepository, CaseService, HybridCaseRetriever, and InvestigationMemoryCoordinator into the Agent. Do not yet remove the case router or case service needed by the legacy Web surface.

Step 5: Run Agent and state tests
Run: python -m pytest tests/agent --ignore=tests/agent/test_memory_integration.py -q

Step 6: Commit the runtime cut-off
```bash
git add apps/control-plane/src/incidentlens_control_plane/agent apps/control-plane/src/incidentlens_control_plane/main.py tests/agent
git commit -m "refactor: remove rag from agent runtime"
```

IMPORTANT: Actually implement all the code changes. Read the existing files first to understand the current structure, then make the necessary modifications.`,
{label: 'task1-cut-rag', phase: 'Phase 1: Foundation'}
)

// Task 2: Add Memory domain types, configuration, and safe file store
log('Starting Task 2: Add Memory domain types, configuration, and safe file store')

const task2Result = await agent(`Execute Task 2: Add Memory domain types, configuration, and safe file store

You are working on the incidentlens project at /Users/luoyu/Documents/incidentlens.

Your task is to create the new project memory package with domain types and a safe file store.

Step 1: Create directory structure
- Create .incidentlens/memory/ directory
- Create apps/control-plane/src/incidentlens_control_plane/project_memory/ package
- Create tests/project_memory/ directory

Step 2: Create .incidentlens/memory/MEMORY.md
This is the committed project Memory index. Start with a simple template.

Step 3: Create .incidentlens/memory/memory-guidelines.md
This file should state that project Memory is reference context, forbids secrets/current telemetry/hidden reasoning, and documents the four types (project, procedure, feedback, reference).

Step 4: Update .gitignore
Add these ignore patterns:
```
.incidentlens/sessions/
.incidentlens/task-outputs/
.incidentlens/transcripts/
```

Step 5: Create apps/control-plane/src/incidentlens_control_plane/project_memory/__init__.py

Step 6: Create apps/control-plane/src/incidentlens_control_plane/project_memory/domain.py
Implement these Pydantic types:
- MemoryType (StrEnum): PROJECT, PROCEDURE, FEEDBACK, REFERENCE
- MemoryLimits: immutable model with hard defaults for all limits
- MemoryCandidate: with name (pattern-validated), description, type, body fields
- MemoryRecord: for stored memory files
- MemoryCatalogEntry: for index entries
- MemoryWriteResult: for write operation results
- LoadedMemories: for loaded memory content

Step 7: Create apps/control-plane/src/incidentlens_control_plane/project_memory/store.py
Implement ProjectMemoryStore with:
- scan(): scans memory directory, returns records
- catalog(): returns MEMORY.md index entries
- write(): atomic write with frontmatter
- load(): bounded loading with path containment checks
- rebuild_index(): rebuilds MEMORY.md from records
- All operations use yaml.safe_load, resolved-path containment checks, atomic writes

Step 8: Modify apps/control-plane/src/incidentlens_control_plane/llm/config.py
Add context_window_tokens and reserved_output_tokens fields to ModelProfile.

Step 9: Create test files
- tests/project_memory/__init__.py
- tests/project_memory/test_domain.py
- tests/project_memory/test_store.py

Step 10: Run tests
Run: python -m pytest tests/project_memory/test_domain.py tests/project_memory/test_store.py tests/llm/test_config.py -q

Step 11: Run lint
Run: python -m ruff check apps/control-plane/src/incidentlens_control_plane/project_memory tests/project_memory apps/control-plane/src/incidentlens_control_plane/llm/config.py

Step 12: Commit
```bash
git add .incidentlens .gitignore config/models.yaml apps/control-plane/src/incidentlens_control_plane/project_memory apps/control-plane/src/incidentlens_control_plane/llm/config.py tests/project_memory tests/llm/test_config.py
git commit -m "feat: add file backed project memory"
```

IMPORTANT: Actually implement all the code. Read existing files first to understand patterns, then create the new files.`,
{label: 'task2-memory-domain', phase: 'Phase 1: Foundation'}
)

// Task 3: Add relevant Memory selection and safe current-turn injection
log('Starting Task 3: Add relevant Memory selection and safe current-turn injection')

const task3Result = await agent(`Execute Task 3: Add relevant Memory selection and safe current-turn injection

You are working on the incidentlens project at /Users/luoyu/Documents/incidentlens.

Your task is to implement memory selection and middleware injection.

Step 1: Create apps/control-plane/src/incidentlens_control_plane/project_memory/selector.py
Implement:
- MemoryQuery(alert_summary, recent_text) dataclass
- MemorySelection(filenames, mode, reason) dataclass
- async select_memories(query, catalog, model, limit=5) -> MemorySelection
  - Model side-query returns JSON with selected_memories
  - Validate against catalog, deduplicate
  - Keyword fallback with Unicode normalization, tokenization, scoring

Step 2: Create apps/control-plane/src/incidentlens_control_plane/project_memory/middleware.py
Implement ProjectMemoryMiddleware for LangChain Agent middleware:
- Injects PROJECT MEMORY — UNTRUSTED REFERENCE boundary
- States it is not Evidence
- Limits to 5 files, 200 lines each, 4KB each, 60KB total
- Maintains content hashes to avoid re-injecting unchanged files

Step 3: Modify apps/control-plane/src/incidentlens_control_plane/project_memory/domain.py
Add any additional types needed for selection.

Step 4: Create tests/project_memory/test_selector.py
Write tests for:
- Model selection validation and limiting
- Keyword fallback behavior
- Catalog validation

Step 5: Create tests/project_memory/test_middleware.py
Write tests for:
- Injection boundary markers
- File count limits
- System instructions not mutated

Step 6: Run tests
Run: python -m pytest tests/project_memory/test_selector.py tests/project_memory/test_middleware.py -q

Step 7: Commit
```bash
git add apps/control-plane/src/incidentlens_control_plane/project_memory tests/project_memory
git commit -m "feat: select and inject relevant project memory"
```

IMPORTANT: Actually implement all the code. Read existing files first to understand patterns.`,
{label: 'task3-memory-selection', phase: 'Phase 1: Foundation'}
)

// =============================================================================
// Phase 2: Compaction (Tasks 4-6)
// =============================================================================

phase('Phase 2: Compaction')

// Task 4: Add deterministic Session Memory
log('Starting Task 4: Add deterministic Session Memory')

const task4Result = await agent(`Execute Task 4: Add deterministic Session Memory

You are working on the incidentlens project at /Users/luoyu/Documents/incidentlens.

Your task is to implement deterministic session memory projection and persistence.

Step 1: Create apps/control-plane/src/incidentlens_control_plane/compaction/__init__.py

Step 2: Create apps/control-plane/src/incidentlens_control_plane/compaction/domain.py
Implement compaction configuration, outcomes, and errors.

Step 3: Create apps/control-plane/src/incidentlens_control_plane/compaction/session.py
Implement:
- SessionMemorySnapshot: projects Objective, verified facts, rejected directions, loaded Skills, completed work, next action, constraints, output references, budget, recoverable errors
- SessionMemoryStore: atomic per-incident persistence to .incidentlens/sessions/
- project_session_memory(state, messages) -> SessionMemorySnapshot: deterministic, no model calls
- validate_session_memory(snapshot, evidence_ids) -> SessionMemoryValidation

Step 4: Modify apps/control-plane/src/incidentlens_control_plane/agent/types.py
Add state fields: session_memory_path, session_memory_revision, last_compaction_id, pre_compact_transcript_path

Step 5: Create tests/compaction/__init__.py

Step 6: Create tests/compaction/test_session.py
Write tests for:
- Projection preserves exact evidence IDs
- Missing evidence reference disables fast compaction
- Atomic persistence

Step 7: Run tests
Run: python -m pytest tests/compaction/test_session.py tests/agent/test_langgraph_state.py -q

Step 8: Commit
```bash
git add apps/control-plane/src/incidentlens_control_plane/compaction apps/control-plane/src/incidentlens_control_plane/agent/types.py tests/compaction
git commit -m "feat: persist deterministic session memory"
```

IMPORTANT: Actually implement all the code. Read existing files first.`,
{label: 'task4-session-memory', phase: 'Phase 2: Compaction'}
)

// Task 5: Implement zero-model compaction layers
log('Starting Task 5: Implement zero-model compaction layers')

const task5Result = await agent(`Execute Task 5: Implement zero-model compaction layers

You are working on the incidentlens project at /Users/luoyu/Documents/incidentlens.

Your task is to implement tool budget persistence, micro compaction, and middle snipping.

Step 1: Create apps/control-plane/src/incidentlens_control_plane/compaction/tool_budget.py
Implement:
- ToolOutputStore: persists oversized tool results to disk
- persist_oversized_tool_results(messages, incident_id, store, limits) -> CompactionResult
  - Persist largest results first until under 128KB
  - Write bytes atomically, calculate SHA-256
  - Return structured reference with path, size, digest, preview, reread instruction

Step 2: Create apps/control-plane/src/incidentlens_control_plane/compaction/micro.py
Implement:
- MessageGroup: collects AIMessage.tool_calls with matching ToolMessage.tool_call_id
- micro_compact(messages, keep_recent=3) -> CompactionResult
  - Replace only completed old result payloads
  - Keep 3 most recent complete tool results
- snip_middle(messages, target_tokens) -> CompactionResult
  - Snip complete middle groups
  - Preserve initial objective and recent groups
  - Insert bounded marker with removed counts

Step 3: Modify apps/control-plane/src/incidentlens_control_plane/compaction/domain.py
Add CompactionLimits and CompactionResult types.

Step 4: Create tests/compaction/test_tool_budget.py
Write tests for:
- Single large result persistence
- SHA-256 verification
- Preview size limits

Step 5: Create tests/compaction/test_micro.py
Write tests for:
- Micro compact keeps 3 recent complete results
- Snip never splits tool group

Step 6: Run tests
Run: python -m pytest tests/compaction/test_tool_budget.py tests/compaction/test_micro.py -q

Step 7: Commit
```bash
git add apps/control-plane/src/incidentlens_control_plane/compaction tests/compaction
git commit -m "feat: add zero model context compaction"
```

IMPORTANT: Actually implement all the code. Read existing files first.`,
{label: 'task5-zero-model-compaction', phase: 'Phase 2: Compaction'}
)

// Task 6: Add Session fast compact, summary fallback, and reactive recovery
log('Starting Task 6: Add Session fast compact, summary fallback, and reactive recovery')

const task6Result = await agent(`Execute Task 6: Add Session fast compact, summary fallback, and reactive recovery

You are working on the incidentlens project at /Users/luoyu/Documents/incidentlens.

Your task is to implement summary fallback, compaction middleware, and reactive recovery.

Step 1: Create apps/control-plane/src/incidentlens_control_plane/compaction/summary.py
Implement:
- SummaryCircuitBreaker(max_failures=3): tracks failures, opens circuit
- async summarize_history(messages, session_memory, model) -> SummaryResult
  - Text-only prompt with "TEXT ONLY; DO NOT CALL TOOLS"
  - Validates objective and Evidence IDs in summary
  - Rejects incomplete summaries

Step 2: Create apps/control-plane/src/incidentlens_control_plane/compaction/middleware.py
Implement:
- CompactionMiddleware(runtime, limits): fixed-order pre-model compaction
  - Persist JSONL transcript
  - Update Session Memory
  - Apply tool budget, middle snip, micro compact
  - Compute threshold: model.context_window_tokens - model.reserved_output_tokens - 13_000
  - Replace checkpoint messages using LangGraph removal semantics
- is_prompt_too_long(exc) helper: normalizes provider errors
- Reactive recovery: retry with smaller budgets, max 2 retries

Step 3: Modify apps/control-plane/src/incidentlens_control_plane/compaction/domain.py
Add any additional types needed.

Step 4: Create tests/compaction/test_summary.py
Write tests for:
- Complete session memory skips summary model
- Three summary failures open circuit

Step 5: Create tests/compaction/test_middleware.py
Write tests for:
- Prompt-too-long recovery (2 retries max)
- Evidence and objective preservation

Step 6: Run tests
Run: python -m pytest tests/compaction -q

Step 7: Commit
```bash
git add apps/control-plane/src/incidentlens_control_plane/compaction tests/compaction
git commit -m "feat: add recoverable context compaction"
```

IMPORTANT: Actually implement all the code. Read existing files first.`,
{label: 'task6-summary-recovery', phase: 'Phase 2: Compaction'}
)

// =============================================================================
// Phase 3: Extraction & Wiring (Tasks 7-8)
// =============================================================================

phase('Phase 3: Extraction & Wiring')

// Task 7: Add stop-hook extraction, bounded tasks, and Dream
log('Starting Task 7: Add stop-hook extraction, bounded tasks, and Dream')

const task7Result = await agent(`Execute Task 7: Add stop-hook extraction, bounded tasks, and Dream

You are working on the incidentlens project at /Users/luoyu/Documents/incidentlens.

Your task is to implement memory extraction, Dream consolidation, and bounded task supervision.

Step 1: Create apps/control-plane/src/incidentlens_control_plane/project_memory/extractor.py
Implement:
- async extract_memories(transcript_path, catalog, model) -> list[MemoryCandidate]
  - Read bounded pre-compact transcript file and catalog
  - Require JSON array of exact candidate fields
  - Apply secret scanning before semantic dedupe
  - Same-name equivalent content updates updated_at
  - Conflicts receive stable -conflict-<content-hash-prefix> suffix

Step 2: Create apps/control-plane/src/incidentlens_control_plane/project_memory/dream.py
Implement:
- DreamGate.evaluate(now, turn_count, lock) -> DreamDecision
  - Requires 24 hours since last successful run
  - Requires 5 completed Agent turns
  - Scan throttling
  - Project lock that expires after 1 hour
- Dream transaction:
  - Acquire .consolidate-lock with exclusive creation
  - Validate all proposed consolidated records
  - Write staging directory, fsync
  - Replace individual target files only after complete proposal validates
  - On failure, leave original files and index unchanged

Step 3: Create apps/control-plane/src/incidentlens_control_plane/project_memory/runtime.py
Implement:
- MemoryTaskSupervisor: bounded task supervisor
  - asyncio.Queue with explicit task priority
  - Named worker tasks
  - Track tasks in set, consume exceptions
  - Stop submission idempotent by (incident_id, turn_id)
  - start(), submit(), close(timeout_seconds)
- ProjectMemoryRuntime: per-turn orchestration
  - on_turn_start(): prefetch memory
  - on_turn_stop(): submit extraction

Step 4: Create tests/project_memory/test_extractor.py
Write tests for:
- Duplicate candidate does not grow store
- Candidate with secret is rejected

Step 5: Create tests/project_memory/test_dream.py
Write tests for:
- Each skip reason separately
- Lock older than one hour
- Concurrent acquisition with exactly one winner

Step 6: Create tests/project_memory/test_runtime.py
Write tests for:
- on_turn_stop() returns before blocked extractor finishes
- Duplicate turn IDs enqueue once
- Dream is dropped before extraction when queue is full
- close() waits only up to timeout

Step 7: Run tests
Run: python -m pytest tests/project_memory -q

Step 8: Commit
```bash
git add apps/control-plane/src/incidentlens_control_plane/project_memory tests/project_memory
git commit -m "feat: extract and consolidate project memory"
```

IMPORTANT: Actually implement all the code. Read existing files first.`,
{label: 'task7-extraction-dream', phase: 'Phase 3: Extraction & Wiring'}
)

// Task 8: Wire Memory and compaction into Agent lifecycle
log('Starting Task 8: Wire Memory and compaction into Agent lifecycle')

const task8Result = await agent(`Execute Task 8: Wire Memory and compaction into Agent lifecycle

You are working on the incidentlens project at /Users/luoyu/Documents/incidentlens.

Your task is to wire the new memory and compaction systems into the Agent lifecycle.

Step 1: Modify apps/control-plane/src/incidentlens_control_plane/agent/factory.py
Wire project_memory_runtime and compaction_runtime into build_investigation_engine.

Step 2: Modify apps/control-plane/src/incidentlens_control_plane/agent/runtime.py
Install middleware in deterministic order:
1. Project filesystem/Skill middleware
2. Project Memory injection
3. Investigation context
4. Audit/evidence/conclusion gates
5. Compaction
6. Report gate

Step 3: Modify apps/control-plane/src/incidentlens_control_plane/agent/graph.py
Wire turn start, compact, and stop hooks.

Step 4: Modify apps/control-plane/src/incidentlens_control_plane/agent/prompts.py
Remove any remaining historical case references.

Step 5: Modify apps/control-plane/src/incidentlens_control_plane/main.py
- Resolve paths from environment variables:
  INCIDENTLENS_MEMORY_DIR=/app/.incidentlens/memory
  INCIDENTLENS_SESSION_DIR=/data/incidentlens/sessions
  INCIDENTLENS_TASK_OUTPUT_DIR=/data/incidentlens/task-outputs
  INCIDENTLENS_TRANSCRIPT_DIR=/data/incidentlens/transcripts
- Start and close MemoryTaskSupervisor in FastAPI lifespan
- For local non-Compose, default paths beneath .incidentlens/

Step 6: Modify apps/control-plane/src/incidentlens_control_plane/llm/registry.py
Expose context_window_tokens and reserved_output_tokens in ModelIdentity.

Step 7: Modify infra/compose/compose.yaml
Mount repository Memory read-write at /app/.incidentlens/memory
Keep session/output/transcript data on named data volume.

Step 8: Create tests/agent/test_memory_compaction_integration.py
Write tests for:
- Middleware order verification
- FastAPI shutdown calls Memory runtime close exactly once

Step 9: Run tests
Run: python -m pytest tests/agent tests/project_memory tests/compaction --ignore=tests/agent/test_memory_integration.py -q

Step 10: Commit
```bash
git add apps/control-plane/src/incidentlens_control_plane/agent apps/control-plane/src/incidentlens_control_plane/main.py apps/control-plane/src/incidentlens_control_plane/llm infra/compose/compose.yaml tests/agent
git commit -m "feat: wire memory and compaction runtime"
```

IMPORTANT: Actually implement all the code. Read existing files first.`,
{label: 'task8-wire-lifecycle', phase: 'Phase 3: Extraction & Wiring'}
)

// =============================================================================
// Phase 4: Cleanup & Verification (Tasks 9-11)
// =============================================================================

phase('Phase 4: Cleanup & Verification')

// Task 9: Retire legacy RAG product code without dropping old data
log('Starting Task 9: Retire legacy RAG product code without dropping old data')

const task9Result = await agent(`Execute Task 9: Retire legacy RAG product code without dropping old data

You are working on the incidentlens project at /Users/luoyu/Documents/incidentlens.

Your task is to remove legacy RAG code while preserving existing database tables.

Step 1: Write route retirement and export tests
Create tests/web/test_rag_routes_retired.py:
```python
async def test_case_routes_are_not_registered(agent_api_client) -> None:
    response = await agent_api_client.get("/api/cases/search", params={"q": "timeout"})
    assert response.status_code == 404

async def test_export_contains_no_case_payload(export_client) -> None:
    body = (await export_client.get("/api/investigations/inc-api/export")).json()
    assert "case" not in body
    assert "case_usage" not in body
```

Step 2: Write non-destructive startup and purge tests
Create tests/services/test_legacy_rag_purge.py

Step 3: Delete RAG package and callers
- Delete apps/control-plane/src/incidentlens_control_plane/memory/
- Delete apps/control-plane/src/incidentlens_control_plane/routes/cases.py
- Modify main.py to remove case router registration
- Remove case fields from API responses
- Remove case service from exports

Step 4: Implement purge script
Create scripts/purge_legacy_rag.py with allowlisted tables:
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
Dry-run by default, requires --confirm-drop-legacy-rag flag.

Step 5: Remove obsolete tests
Delete tests/web/test_case_governance_api.py
Delete tests/agent/test_memory_integration.py
Delete all legacy RAG tests under tests/memory/

Step 6: Run static search
Run: rg -n "HybridCaseRetriever|InvestigationMemoryCoordinator|CaseRepository|CaseService|retrieved_cases|case_status" apps scripts

Step 7: Run tests
Run: python -m pytest tests/web tests/services tests/agent -q

Step 8: Commit
```bash
git add -A apps/control-plane/src tests scripts
git commit -m "refactor: retire legacy rag product surface"
```

IMPORTANT: Actually implement all the code. Read existing files first.`,
{label: 'task9-retire-rag', phase: 'Phase 4: Cleanup & Verification'}
)

// Task 10: Remove RAG evaluation/UI semantics and update product documentation
log('Starting Task 10: Remove RAG evaluation/UI semantics and update product documentation')

const task10Result = await agent(`Execute Task 10: Remove RAG evaluation/UI semantics and update product documentation

You are working on the incidentlens project at /Users/luoyu/Documents/incidentlens.

Your task is to update evaluation, UI, and documentation to remove RAG references.

Step 1: Rewrite evaluation tests
Modify tests/evaluation/test_metrics.py:
- Assert EVALUATION_STRATEGIES == {"deterministic_baseline", "llm_agent"}
- Add project_memories_loaded, compaction_count, summary_fallback_count fields

Step 2: Rewrite dashboard contract tests
Modify tests/web/test_dashboard_contract.py:
- Assert no case queue/editor/search/history/feedback regions
- Assert no /api/cases strings
- Preserve investigation timeline, Evidence, export, scenarios, evaluation results

Step 3: Simplify evaluation runner and metrics
Modify packages/evaluation/src/incidentlens_evaluation/runner.py:
- Remove CaseRepository, CaseRow, historical seeding
- Select engine mode from two runtime strategy names

Modify packages/evaluation/src/incidentlens_evaluation/metrics.py:
- Remove historical_cases_adopted, historical_cases_misleading
- Add project_memories_loaded, compaction_count, summary_fallback_count

Step 4: Remove case governance dashboard
Modify apps/control-plane/static/index.html and assets:
- Remove case API requests, governance controls, case scoring labels
- Keep existing visual language and navigation

Step 5: Update documentation
Create docs/memory-and-compaction.md documenting:
- File formats, Git policy, limits
- Compose paths, metrics, failure modes
- Explicit purge command

Modify README.md and REQUIREMENTS.md:
- Replace FR-06/FR-07 with project Memory requirements
- Remove RAG claims

Step 6: Run static content checks
Run: rg -n "RAG|Embedding|HybridCaseRetriever|case memory|historical case" README.md REQUIREMENTS.md docs packages/evaluation apps/control-plane/static

Step 7: Run tests
Run: python -m pytest tests/evaluation tests/web/test_dashboard_contract.py -q

Step 8: Commit
```bash
git add packages/evaluation tests/evaluation apps/control-plane/static tests/web/test_dashboard_contract.py README.md REQUIREMENTS.md docs tests/test_test_topology.py
git commit -m "docs: replace rag product semantics with memory"
```

IMPORTANT: Actually implement all the code. Read existing files first.`,
{label: 'task10-update-docs', phase: 'Phase 4: Cleanup & Verification'}
)

// Task 11: Add end-to-end Memory/compact acceptance and run all gates
log('Starting Task 11: Add end-to-end Memory/compact acceptance and run all gates')

const task11Result = await agent(`Execute Task 11: Add end-to-end Memory/compact acceptance and run all gates

You are working on the incidentlens project at /Users/luoyu/Documents/incidentlens.

Your task is to create integration tests and run all quality gates.

Step 1: Create tests/integration/test_memory_compaction_flow.py
Implement integration acceptance test that:
1. Starts an investigation that discovers a stable project procedure
2. Completes a normal Agent turn and waits for bounded extraction hook
3. Asserts one Markdown Memory and valid MEMORY.md index exist
4. Starts a related investigation and asserts file is selected
5. Forces oversized tool output and verifies persisted output reference
6. Restarts control plane and resumes same incident
7. Asserts Evidence IDs and loaded Skill names unchanged
8. Asserts no successful tool call is repeated
9. Asserts no query touches legacy RAG tables

Step 2: Delete tests/integration/test_memory_governance_flow.py

Step 3: Modify tests/test_test_topology.py
Update so new integration module is required to declare pytestmark = pytest.mark.integration

Step 4: Run all non-network tests
Run: python -m pytest -m 'not integration and not live_llm' -q

Step 5: Run lint and type checks
Run: python -m ruff check . --exclude .claude
Run: python -m mypy apps packages

Step 6: Run static retirement checks
Run: rg -n "from incidentlens_control_plane\.memory|HybridCaseRetriever|InvestigationMemoryCoordinator|CaseRepository|CaseService|retrieved_cases" apps packages scripts tests

Step 7: Run purge and retired routes tests
Run: python -m pytest tests/services/test_legacy_rag_purge.py tests/web/test_rag_routes_retired.py -q

Step 8: Update verification evidence
Record results in docs/phase-5-live-verification.md

Step 9: Commit
```bash
git add tests/integration tests/test_test_topology.py docs/phase-5-live-verification.md
git commit -m "test: verify memory and compaction redesign"
```

IMPORTANT: Actually implement all the code. Read existing files first.`,
{label: 'task11-acceptance', phase: 'Phase 4: Cleanup & Verification'}
)

// =============================================================================
// Final Summary
// =============================================================================

log('All 11 tasks completed!')
log('Task 1 (Cut RAG): ' + (task1Result ? '✓' : '✗'))
log('Task 2 (Memory Domain): ' + (task2Result ? '✓' : '✗'))
log('Task 3 (Memory Selection): ' + (task3Result ? '✓' : '✗'))
log('Task 4 (Session Memory): ' + (task4Result ? '✓' : '✗'))
log('Task 5 (Zero-Model Compaction): ' + (task5Result ? '✓' : '✗'))
log('Task 6 (Summary Recovery): ' + (task6Result ? '✓' : '✗'))
log('Task 7 (Extraction Dream): ' + (task7Result ? '✓' : '✗'))
log('Task 8 (Wire Lifecycle): ' + (task8Result ? '✓' : '✗'))
log('Task 9 (Retire RAG): ' + (task9Result ? '✓' : '✗'))
log('Task 10 (Update Docs): ' + (task10Result ? '✓' : '✗'))
log('Task 11 (Acceptance): ' + (task11Result ? '✓' : '✗'))

return {
  task1: task1Result,
  task2: task2Result,
  task3: task3Result,
  task4: task4Result,
  task5: task5Result,
  task6: task6Result,
  task7: task7Result,
  task8: task8Result,
  task9: task9Result,
  task10: task10Result,
  task11: task11Result,
}
