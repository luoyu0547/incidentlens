# Task 2 report

## Status
Implemented the deterministic scenario runner in the test-side evaluation package.

## TDD evidence
- RED: `uv run pytest tests/eval/test_harness_eval.py -q` failed during collection because `tests.eval.runner` was undefined.
- Initial GREEN passed 12 tests but was rejected because scenarios were superficial stop-only scripts.
- Round 1 fix RED exposed fabricated compaction state, incomplete persisted extraction, and approval pairing behavior.
- Round 1 GREEN: focused evaluation suite passed 12 tests.
- Round 2 GREEN: `uv run pytest tests/eval/test_metrics.py tests/eval/test_harness_eval.py -q` passes 12 tests; CLI prints six rows and writes `/tmp/incidentlens-harness-eval.json`.

## Dependency commits
Task 1 dependency commits were cherry-picked and resolved before Task 2: `2ba1629`, `fee40a8`, `733b009`, and `74b5739`.

## Round 2 fix evidence
- Restart now leaves the child receipt genuinely pending (`delivered_at is None`) before creating a second runtime over the same SQLite path. It invokes `RecoveryService.startup()` on that second runtime, asserts `reconciled_child_receipts == 1`, verifies persisted `delivered_at`, and runs a second startup asserting zero additional reconciliation. No parent provider loop runs before recovery.
- `_trace` derives child run IDs from `list_agent_runs(parent_run_id=...)` and receipts by querying the durable receipt store. It no longer accepts caller-supplied expected child IDs. Child metrics therefore cannot be injected by scenario constants.
- All six calls use explicit `AgentBudget` values at run creation. The overflow case additionally uses a smaller explicit budget while retaining the real reactive compactor path.
- Delegation equivalence now extends `HarnessTrace.delegation_forms`; extraction derives typed delegation from persisted delegated-task records and tool delegation from persisted `delegate_child` calls. The scenario asserts both forms and equivalent delivered child receipts, rather than discarding the alternate trace.
- The prior real compactor, scope rejection, approval, and evidence grounding behavior is preserved.

## Files
- `/Users/chenxueqiang/Documents/code/incidentlens/.claude/worktrees/agent-harness-eval/tests/eval/support.py`
- `/Users/chenxueqiang/Documents/code/incidentlens/.claude/worktrees/agent-harness-eval/tests/eval/scenarios.py`
- `/Users/chenxueqiang/Documents/code/incidentlens/.claude/worktrees/agent-harness-eval/tests/eval/runner.py`
- `/Users/chenxueqiang/Documents/code/incidentlens/.claude/worktrees/agent-harness-eval/tests/eval/types.py`
- `/Users/chenxueqiang/Documents/code/incidentlens/.claude/worktrees/agent-harness-eval/tests/eval/test_harness_eval.py`

## Verification
Focused tests pass. The runner CLI produces six scenario rows and JSON. `git diff --check` is clean. Full-suite and final lint execution are required before the round-2 commit; the environment rejected the worktree-isolated Bash lint wrapper, so the final coordinator should rerun the exact project commands if needed.
