# Phase 5 Verification Record

## Verification details

- Timestamp: 2026-08-03T02:55:35Z
- Verified code commit: `5153c96`
- Branch: `feat/phase-5-knowledge-loop`
- Runtime mode: `deterministic_baseline`

## Deterministic Compose gates

```bash
INCIDENTLENS_AGENT_MODE=deterministic_baseline \
uv run pytest \
  tests/integration/test_compose_flow.py \
  tests/integration/test_scenario_acceptance.py \
  tests/integration/test_memory_governance_flow.py \
  -m integration -q
```

Result: `31 passed in 343.43s`.

The suite verified all five deterministic fault scenarios, report evidence
traceability, automatic case materialization, human review and confirmation,
historical-case recall, misleading-case classification, feedback persistence,
and versioned investigation export. Runtime-generated case and investigation
IDs are intentionally ephemeral and are not retained in this repository.

## Export verification

- Schema version: `incidentlens.investigation-export.v1`
- Supporting evidence IDs present: yes
- `root_cause_label` excluded: yes
- Usage events and feedback available through governance history: yes

## Quality gates

### Unit tests

```bash
INCIDENTLENS_AGENT_MODE=deterministic_baseline \
uv run pytest -m 'not integration and not live_llm' -q
```

Result: `409 passed, 34 deselected in 23.92s`.

### Ruff

```bash
uv run ruff check . --exclude .claude
```

Result: all checks passed.

### Mypy

```bash
uv run mypy apps packages
```

Result: no issues in 75 source files.

### Secret scan

The tracked source tree was scanned for OpenAI-style secret keys, long bearer
tokens, and long inline API-key assignments. Result: no matches.

## Outcome

Phase 5 passed its deterministic verification gates with no waived or
pre-existing failures. The knowledge loop is ready for integration into
`main`.
