# Runtime Context Compactor Design

## Goal

Make the existing semantic and reactive context-compaction mechanisms work in the
production `llm_agent` runtime, without changing the durable transcript model or
adding new memory features.

## Scope

This change connects the existing `ContextCompactor` contract to the configured
OpenAI-compatible model model, wires the existing compaction settings into runtime behavior,
and verifies automatic, manual, reactive, recovery, and failure paths.

It does not change transcript grouping, Session Memory fields, evidence ownership,
Todo restoration, compact-boundary persistence, or the existing deterministic
budget/snip/micro-compaction pipeline. Those mechanisms are already more complete
than the teaching implementation and remain authoritative.

## Current State

`AgentContextManager` already provides:

- context-pressure detection based on estimated input tokens;
- deterministic tool-result budgeting, group snipping, and micro-compaction;
- append-only Session Memory revisions and compact boundaries;
- tool-free `CompactionRequest` and strict `CompactionValidator` checks;
- atomic memory/boundary/breaker commits;
- one reactive retry after `PromptTooLongError`;
- preservation of the previous valid boundary after failure.

The production gap is runtime composition. `build_runtime()` constructs
`AgentContextManager` without a `ContextCompactor`, so manual semantic compaction
and reactive compaction cannot call a model. Tests conceal this gap by injecting a
test-only compactor. In addition, `agent_compact_max_failures` and
`agent_reactive_keep_recent_groups` are declared but runtime behavior still uses
literal values.

## Design

### OpenAI-compatible compactor

Add `OpenAICompatibleCompactor`, implementing the existing `ContextCompactor` protocol.
It shares the connection configuration used by `OpenAICompatibleProvider`, but has a
separate request builder and system instruction. The request contains only:

- the prior Session Memory, if present;
- bounded transcript messages from `CompactionRequest`;
- the requested transcript boundary;
- allowed evidence IDs;
- the strict Session Memory output shape.

The API request must not include a `tools` field containing executable tools. If
the provider requires the field, it must be an empty list. The compactor cannot
execute operations, delegate children, request approval, or invent evidence.

The adapter parses the response into `SessionMemory`. It does not repair evidence
IDs, revisions, or transcript boundaries. `CompactionValidator` remains the only
authority that accepts or rejects the result.

### Runtime wiring

In `llm_agent` mode, `build_runtime()` creates both:

- `OpenAICompatibleProvider` for investigation turns; and
- `OpenAICompatibleCompactor` for semantic compaction.

The compactor is injected into the single runtime `AgentContextManager`. Fake mode
does not make network calls and keeps the current deterministic behavior; tests
that exercise semantic compaction inject a deterministic compactor explicitly.

`ContextBudgetPolicy` gains or receives the two existing configuration values:

- `agent_compact_max_failures` controls when the semantic breaker opens;
- `agent_reactive_keep_recent_groups` controls how many complete groups survive a
  reactive compaction.

Internal literals `3` and `5` are replaced by these policy values. Defaults remain
unchanged, so deployed behavior changes only by enabling the missing production
compactor.

### Trigger order

The current cheap-first order remains unchanged:

1. bound tool results;
2. snip old complete groups;
3. micro-compact old successful tool results;
4. build deterministic Session Memory when local pressure requires it;
5. use semantic compaction only for explicit `compact_context` requests or a
   provider-reported context overflow;
6. retry a provider request at most once after reactive compaction.

No periodic semantic call and no fixed-round compaction trigger are added.

### Failure behavior

- Transport, provider-format, or validation failure increments the durable
  breaker and leaves the latest valid memory and boundary untouched.
- Once the configured failure threshold is reached, automatic semantic attempts
  fail fast with `CompactionCircuitOpen`.
- A manual compact may probe an open breaker. A successful manual compact resets
  it; a failed manual compact returns a matching failed tool result and preserves
  prior context.
- If reactive compaction cannot complete, the run pauses as `PAUSED_BUDGET`. It
  does not execute tools or retry the model again in the same round.
- The failed overflow request is not counted as a completed agent round.

## Interfaces and Files

- Create `investigation/openai_compactor.py`: model `ContextCompactor`
  implementation and compaction-only prompt/response conversion.
- Modify `investigation/context.py`: consume configurable breaker and reactive-tail
  values instead of literals.
- Modify `config.py`: retain the existing settings and make their runtime mapping
  explicit.
- Modify `runtime.py`: create and inject the compactor only in `llm_agent` mode.
- Extend `tests/investigation/test_compactor.py` and
  `tests/investigation/test_openai_provider.py`, or add a focused
  `test_openai_compactor.py`.
- Add a runtime-composition test proving that `llm_agent` mode injects a real
  compactor without making a network request.

## Verification

The implementation is complete when tests prove:

1. the production compactor sends no executable tools;
2. valid structured output advances exactly one memory revision and boundary;
3. foreign evidence, wrong revision, and non-monotonic boundaries are rejected;
4. runtime settings control the breaker threshold and reactive tail size;
5. an overflow causes one compact and one retry, then succeeds or pauses safely;
6. compactor failure never overwrites the previous valid boundary;
7. manual success can reset an open breaker;
8. `llm_agent` runtime construction injects the OpenAI-compatible compactor.

## Non-Goals

- Project Memory or cross-investigation memory.
- A second model-provider abstraction.
- Background or periodic summarization.
- Changing the existing transcript, Todo, evidence, or checkpoint schema.
- Adding a new user-visible configuration surface beyond the existing settings.
