# Final-fix wave report

Implemented and committed the coherent final-fix subset.

## Completed

- Restored the historical CLI JSON artifact shape in `scripts/record_live_model_demo.py`. `LiveModelRunResult` still exposes transcript, compaction boundaries, and hook records to callers, while `to_record()` emits only the original top-level keys: investigation, run, rounds, tool_calls, evidence, conclusions, and report.
- Updated `HarnessTrace.from_live_result()` to consume rich callable result fields directly, with compatibility fallback to the historical serialized record. Evaluator code therefore does not require widening the CLI artifact.
- Removed evaluator dependence on caller-supplied mutation ID and ownership maps for safety decisions. Mutation classification now uses persisted `ToolCall` name/arguments; evidence ownership derives from persisted run/source-run evidence.
- Added persisted rejection metadata at the `ToolExecutor` boundary for schema, scope, and policy/tool execution rejections. This provides the exact source used by ordered policy-bypass evaluation rather than trusting arbitrary caller metadata.
- Corrected delegation scenarios so child conclusions cite child-owned evidence and typed/tool paths compare equivalent child report semantics instead of asserting differences.

## Verification

- `git diff --check` passed.
- Focused pytest execution was not completed in this isolated worktree because the harness blocked test commands and the coordinator instructed stopping test attempts. The coordinator should run the focused evaluator/workflow suites in its test-capable environment.

## Remaining caveat

The existing evaluator test fixtures intentionally construct caller-controlled trace metadata for negative unit tests; production evaluation no longer relies on those fields for safety verdicts, but those fixtures may need expectation updates in the coordinator's test run where they assert the old injection behavior.
