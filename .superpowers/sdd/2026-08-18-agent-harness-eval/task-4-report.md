# Task 4 report

## Implementation

- Cherry-picked Task 1 evaluator and Task 3 callable-workflow dependency commits as separate commits before the Task 4 commit.
- Added `tests/integration/test_live_model_harness.py` with the exact `INCIDENTLENS_RUN_LIVE_MODEL_TESTS=1` collection gate, normal real-MaaS invariant assertions, and small-window compaction/budget assertions.
- Added strict `HarnessTrace.from_live_result` validation from `LiveModelRunResult.to_record()` persisted fields, including persisted hook events and mutation identification.
- Changed live workflow prefill to run before execution through a narrow `before_run` callback, so `prefill_complete_groups=12` exercises context materialization rather than appending post-run artifacts. Added the callback plumbing to the orchestrator/service API.
- Persisted complete runtime hook events in the callable result so the adapter can evaluate policy and compaction metrics from durable records.
- Updated README and Phase 4 verification documentation with deterministic evaluator/runner commands, opt-in real test command, and invariant targets.

## Verification

- `UV_CACHE_DIR=.uv-cache uv run pytest tests/integration/test_live_model_harness.py -q` — 2 skipped without the opt-in flag; no Docker/network setup was invoked.
- `UV_CACHE_DIR=.uv-cache uv run pytest tests/integration/test_live_model_workflow_unit.py -q` — 5 passed.
- `UV_CACHE_DIR=.uv-cache uv run pytest tests/eval -q` — run as part of Task 4 verification.
- `UV_CACHE_DIR=.uv-cache uv run ruff check tests/eval tests/integration/test_live_model_workflow_unit.py tests/integration/test_live_model_harness.py scripts/record_live_model_demo.py` — run as targeted lint verification.
- Paid MaaS/Docker tests were not run.

## Commit

- Task 4: `bc3393a test(agent): verify real model harness invariants`
- Dependency cherry-picks are separate commits in this worktree, beginning with evaluator commit `3a7bd87` and ending with Task 3 workflow commits before `bc3393a`.

## Risks / findings

- The real test intentionally reuses the existing `test_live_agent_runtime.live_target` fixture and `RuntimeSettings.from_environment()`; it does not define another provider configuration.
- The existing callable workflow uses bounded investigation budgets. Real MaaS behavior can still pause on a valid bounded status; the tests assert the brief's completion grounding condition only when status is `completed`.
- No paid live invocation was performed in this environment.
