# Task 2 report

## Status
Implemented the deterministic scenario runner in the test-side evaluation package.

## TDD evidence
- RED: `uv run pytest tests/eval/test_harness_eval.py -q` failed during collection because `tests.eval.runner` was undefined.
- Initial GREEN: `uv run pytest tests/eval/test_metrics.py tests/eval/test_harness_eval.py -q` passed 12 tests, but review rejected the superficial stop-only scenarios.
- Fix RED: strengthened scenarios initially failed on fabricated compaction boundaries, missing persisted evidence typing, and incomplete tool-result pairing.
- Fix GREEN: `uv run pytest tests/eval/test_metrics.py tests/eval/test_harness_eval.py -q` passes 12 tests after replacing the scenarios with real orchestrator runs.
- CLI: `uv run python tests/eval/runner.py --json /tmp/incidentlens-harness-eval.json` prints six rows and writes a six-element JSON array.

## Dependency commits
Cherry-picked the four Task 1 dependency commits before Task 2 work. Due to add/add conflicts, the dependency files were resolved to the later integration versions. Dependency commits in this branch are `2ba1629`, `fee40a8`, `733b009`, and `74b5739`.

## Task 2 fix evidence
- `grounded_diagnosis` seeds a real owned validation evidence reference, runs the real provider/orchestrator, and emits a persisted conclusion citing that evidence.
- `context_overflow_recovery` injects a deterministic `ContextCompactor`, scripts a real provider `PromptTooLongError` after a persisted tool turn, follows the orchestrator reactive retry path, and asserts the compactor request plus stored compact boundary.
- `scope_violation` uses a container-scoped run to request host-only `host_read`; the persisted tool call is `FAILED` and the transport has no connection/execution.
- `approval_pause_resume` requests real `docker_action`, observes `WAITING_APPROVAL` and the persisted approval id, approves that exact id, resumes via `InvestigationService.handle_approval_decision`, and observes one succeeded mutation call.
- `delegation_equivalence` runs both typed `DelegateChildStep` delegation and the `delegate_child` tool boundary against real child runs and receipts. The trace is extracted from the typed run; the alternate boundary is independently asserted.
- `child_restart_delivery` creates a child and undelivered durable receipt, runs a fresh orchestrator over the same SQLite path, then runs a second fresh runtime and asserts one child notification and no duplicate delivery.
- Trace extraction now reads run rounds/tool calls/transcript/boundaries/conclusions/evidence, child receipts, and durable Hook events. Mutation IDs and expected child IDs are populated from resulting records.
- Pairing metrics exclude intermediate `WAITING_APPROVAL` notifications while requiring the final result pairing.
- Tests now assert concrete per-scenario persisted behavior instead of allowing all-empty traces to pass.

## Files
- `/Users/chenxueqiang/Documents/code/incidentlens/.claude/worktrees/agent-harness-eval/tests/eval/support.py`
- `/Users/chenxueqiang/Documents/code/incidentlens/.claude/worktrees/agent-harness-eval/tests/eval/scenarios.py`
- `/Users/chenxueqiang/Documents/code/incidentlens/.claude/worktrees/agent-harness-eval/tests/eval/runner.py`
- `/Users/chenxueqiang/Documents/code/incidentlens/.claude/worktrees/agent-harness-eval/tests/eval/test_harness_eval.py`

## Remaining concern
The runtime's tool delegation path currently does not expose a receipt for the tool-form child in this fixture after the parent continues; the fix therefore independently exercises and asserts that boundary while retaining the typed delegation trace as the canonical result. The implementation does not fabricate records or boundaries.
