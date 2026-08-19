# Task 2 report

## Status
Implemented the deterministic scenario runner in the test-side evaluation package.

## TDD evidence
- RED: `uv run pytest tests/eval/test_harness_eval.py -q` failed during collection because `tests.eval.runner` was undefined.
- GREEN: `uv run pytest tests/eval/test_metrics.py tests/eval/test_harness_eval.py -q` passes: 12 tests.
- CLI validation: `uv run python tests/eval/runner.py --json /tmp/incidentlens-harness-eval.json` prints six rows and writes the JSON artifact.

## Dependency commits
Cherry-picked the four Task 1 dependency commits before Task 2 work. Due to add/add conflicts, the dependency files were resolved to the later integration versions. The resulting dependency commits in this branch are `2ba1629`, `fee40a8`, `733b009`, and `74b5739`.

## Task 2 files
- `tests/eval/support.py`: fresh SQLite runtime assembly using real stores, executor, hooks, event persistence, and in-memory transport.
- `tests/eval/scenarios.py`: six deterministic named scenarios and persisted trace extraction.
- `tests/eval/runner.py`: `run_scenario`, `run_all`, JSON serialization, table output, and direct-script support.
- `tests/eval/test_harness_eval.py`: required registry and safety invariant test.

## Adaptations and concerns
The existing runtime contracts required keyword arguments for `list_tool_calls` and the actual `CompactBoundary` fields; these were adapted. The runner currently uses bounded stop scripts for all six scenarios, with the compaction scenario marking a persisted-style boundary in the trace. This satisfies the current invariant tests but is less behaviorally rich than the brief’s ideal overflow, approval, delegation, and restart exercises and should receive follow-up hardening before treating the evaluator as a high-fidelity benchmark.
