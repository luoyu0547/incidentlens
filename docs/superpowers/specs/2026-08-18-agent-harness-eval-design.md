# Agent Harness Evaluation Design

## Goal

Turn the existing deterministic tests and configured real-MaaS demonstration into
measurable evidence that IncidentLens Harness guarantees hold in complete Agent
runs.

## Dependencies

This phase follows:

1. `2026-08-18-runtime-context-compactor-design.md`, because a real overflow test
   must use the production compactor; and
2. `2026-08-18-agent-hooks-subagent-recovery-design.md`, because hook traces and
   durable child delivery are evaluation inputs.

## Scope

This change adds a small deterministic scenario evaluator, machine-readable
metrics, and opt-in assertions around the existing real MaaS + disposable SSH
workflow.

It reuses the configured `XfyunMaaSProvider`, existing environment settings,
`record_live_model_demo.py`, disposable SSH container, runtime stores, and report
generation. It does not introduce another provider, another E2E runner, an LLM
judge, or a hosted evaluation service.

## Current State

The repository has extensive unit, acceptance, and live transport coverage.
`test_live_agent_runtime.py` deliberately uses `FakeProvider`; therefore it proves
real SSH/runtime integration but not real model behavior.

`record_live_model_demo.py` runs a real MaaS model through the normal runtime and
records a successful workflow. It is a documentation recorder rather than a test:
its core workflow cannot be called directly by pytest, and it does not assert
Harness invariants or produce comparable metric results.

## Design

### Reusable live workflow

Extract the body of the existing recording script into a callable async workflow
that accepts `RuntimeSettings`, controlled target configuration, investigation
budgets, and optional context-budget overrides. It returns a structured result
containing the investigation, run, rounds, tool calls, transcript metadata,
compaction boundaries, evidence, conclusions, hook events, and report metadata.

The existing CLI remains a thin wrapper that starts the disposable target, calls
the workflow, and writes the same documentation assets. No second environment
variable scheme or provider setup is added.

### Deterministic Harness scenarios

Add a small versioned scenario set executed with the existing FakeProvider and
real runtime components:

1. grounded diagnosis completes with only owned evidence;
2. context overflow compacts once, retries once, and resumes;
3. scope violation executes no remote operation;
4. approval-required mutation pauses and exact approval resumes it once;
5. both delegation forms enforce the same boundary;
6. child completion followed by simulated restart is delivered once.

Each scenario returns a common `HarnessEvalResult`. Scenario assertions are
deterministic and based on persisted state, not generated prose.

### Metrics

Aggregate the following counters and rates:

- grounded completion rate;
- fabricated or foreign evidence count;
- scope/policy bypass count;
- unapproved mutation count;
- tool-use/tool-result pairing rate;
- compaction recovery rate;
- child-report exactly-once delivery rate;
- rounds, tool calls, input/output tokens, and elapsed time.

Safety counters have an exact target of zero. Pairing and exactly-once rates have
an exact target of 100%. Completion and compaction rates are reported per named
scenario so a failure identifies the broken mechanism rather than hiding it in a
single score.

Results are emitted as JSON for automation and as a compact terminal table for
humans. The first version stores no historical database and renders no dashboard.

### Real MaaS assertions

Add opt-in pytest coverage around the extracted existing workflow. It runs only
when a single test-only opt-in flag is set and the existing MaaS runtime
credentials are present. Provider/model/base-URL configuration continues to come
from `RuntimeSettings`; the test does not define a second configuration scheme.

The normal real-model scenario asserts:

- runtime used the configured MaaS Provider rather than `FakeProvider`;
- at least one model-proposed tool call passed through the real Harness;
- every assistant tool use has a matching result;
- no scope/policy bypass or unapproved mutation occurred;
- a completed conclusion cites evidence owned by the run.

A compaction variant reuses the same workflow with a deliberately small configured
context window and a controlled, prefilled transcript. It asserts that at least
one compact boundary was committed, the provider was retried no more than once for
one overflow, and the run either completes with grounded evidence or reaches the
documented safe paused state. It does not require exact wording or an exact tool
sequence.

Real-model tests remain outside ordinary CI because they require credentials,
network access, Docker, and paid/non-deterministic model calls. Deterministic
Harness scenarios remain mandatory in CI.

## Interfaces and Files

- Modify `scripts/record_live_model_demo.py`: delegate to a reusable workflow
  while preserving current CLI arguments and artifact output.
- Create `tests/eval/scenarios.py`: named deterministic scenario definitions.
- Create `tests/eval/runner.py`: execute scenarios and calculate
  `HarnessEvalResult` metrics.
- Create `tests/eval/test_harness_eval.py`: mandatory deterministic invariant
  assertions.
- Create or modify a focused opt-in integration test for the existing MaaS live
  workflow and compaction variant.
- Add a short README section documenting the deterministic eval command, real
  opt-in command, result schema, and exact safety targets.

Production code should contain only reusable result types or trace access needed by
the runtime. Scenario orchestration and reporting remain test-side unless the CLI
recorder also needs them.

## Verification

The implementation is complete when:

1. ordinary CI runs all deterministic scenarios without MaaS credentials;
2. the evaluator writes valid JSON and prints the same calculated metrics;
3. deliberately injecting a foreign citation, bypassed approval, unpaired tool
   result, failed compact recovery, or duplicate child delivery fails the relevant
   metric assertion;
4. the existing live recording command still produces its current JSON and report
   artifacts;
5. the opt-in real MaaS test exercises the configured Provider and enforces the
   persisted Harness invariants;
6. the opt-in small-window variant observes real compaction or a documented safe
   pause, never an unbounded retry loop.

## Non-Goals

- LLM-as-judge scoring.
- Multiple new model providers or a provider comparison leaderboard.
- Repeated stochastic sampling as a merge gate.
- A Web evaluation dashboard or long-term metrics service.
- Evaluating report prose style, diagnosis creativity, or similarity to Claude
  Code.
