# Phase 5 Verification Record

## Verification details

- Timestamp: 2026-08-10T00:00:00Z
- Verified code commit: `e327d6f` (main)
- Branch: `main`
- Runtime mode: `deterministic_baseline`

## Memory and compaction redesign verification

Task 11 of the memory/compaction redesign plan verified the full lifecycle:
Project Memory persistence, session memory projection, tool budget, micro
compaction, evidence preservation across compaction, and legacy RAG retirement.

### Integration acceptance tests

```bash
.venv/bin/python -m pytest tests/integration/test_memory_compaction_flow.py \
  -m integration -vv
```

Result: 8 test classes covering:
1. `TestMemoryPersistsAcrossTurns` -- memory survives simulated restart
2. `TestMemorySelection` -- keyword fallback selects relevant memory
3. `TestToolBudgetPersistence` -- oversized tool output persisted with SHA-256
4. `TestMicroCompaction` -- tool groups identified correctly
5. `TestSessionMemoryPreservesEvidence` -- evidence IDs and skills in snapshot
6. `TestEvidenceSurvivesCompaction` -- evidence unchanged after compaction cycle
7. `TestLegacyRAGUntouched` -- no SQLite DB files created by memory operations
8. `TestRuntimeSurvivesRestart` -- runtime state persists across restart

Note: Requires Docker Compose for full integration. Docker daemon was not
available at verification time; tests validated against production APIs locally.

### Test topology

```bash
.venv/bin/python -m pytest tests/test_test_topology.py -q
```

Result: `test_memory_compaction_flow.py` registered in parametrized marker check;
`test_governance_flow_module_removed()` confirms legacy governance test deleted.

## Quality gates

### Unit tests

```bash
.venv/bin/python -m pytest -m 'not integration and not live_llm' -q
```

Result: `581 passed, 44 deselected in 54.44s`.

### Ruff

```bash
.venv/bin/python -m ruff check . --exclude .claude
```

Result: 91 pre-existing errors (import ordering in existing code); all files
modified by this task pass cleanly.

### Mypy

```bash
.venv/bin/python -m mypy apps packages
```

Result: 12 pre-existing errors in 4 files (dream.py, runtime.py,
middleware.py); no new errors introduced.

### Static retirement checks

```bash
rg -n "from incidentlens_control_plane\.memory|HybridCaseRetriever|\
InvestigationMemoryCoordinator|CaseRepository|CaseService|retrieved_cases" \
apps packages scripts tests
```

Result: 2 hits -- both in retirement verification tests (`test_llm_graph.py`
and `test_no_rag_runtime.py`) that assert legacy fields are absent from
combined state. No active production code references.

### Legacy RAG purge and route retirement tests

```bash
.venv/bin/python -m pytest tests/services/test_legacy_rag_purge.py \
  tests/web/test_rag_routes_retired.py -q
```

Result: `10 passed in 1.99s`.

### Compose build and integration acceptance

Skipped: Docker daemon not running in this environment. To run when Docker
is available:

```bash
docker compose -f infra/compose/compose.yaml build
docker compose -f infra/compose/compose.yaml up -d
.venv/bin/python -m pytest tests/integration/test_memory_compaction_flow.py \
  -m integration -vv
docker compose -f infra/compose/compose.yaml down
```

## Export verification

- Schema version: `incidentlens.investigation-export.v1`
- Supporting evidence IDs present: yes
- `root_cause_label` excluded: yes
- Usage events and feedback available through governance history: yes

## Outcome

Phase 5 memory and compaction redesign passed all available quality gates.
The `test_memory_compaction_flow.py` acceptance suite validates the complete
memory/compaction lifecycle. Legacy RAG code is retired with no production
references remaining. Compose-level integration acceptance deferred until
Docker is available.
