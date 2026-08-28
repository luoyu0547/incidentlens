# Cloud Agent Harness Refactor Design

## Goal

Turn IncidentLens into a Claude Code-style harness for cloud incident discovery,
repair, verification, and recovery. The model owns investigation decisions; the
harness owns context quality, tool execution, permissions, persistence, and
error recovery.

This refactor must remove demonstration-driven workflow control. A successful
run is defined by observable safety and outcome invariants, never by fixed round
numbers or a prescribed tool sequence.

## Scope

The refactor covers five connected concerns:

1. the parent and child agent loop control boundary;
2. shared model transport and error recovery;
3. pressure-driven context compaction and session continuity;
4. automatic local project memory across investigations;
5. outcome-based cloud and context-pressure acceptance tests.

It does not replace the remote operation, approval, changeset, rollback,
evidence, project registry, TUI, event, report, or SQLite persistence systems.

## Retained Safety Foundation

The following existing capabilities remain authoritative:

- project, target, service, and path scope validation;
- SSH, Docker, log, host-file, and evidence tools;
- protected-path approval and project-wide policy resolution;
- precondition hashes, encrypted backups, changesets, verification, and rollback;
- append-only transcript, evidence, Todo, events, reports, and restart recovery;
- exact approval for cloud actions with material external side effects.

Local transcript, compaction, Session Memory, Project Memory, indexes, and
derived audit data do not require human approval. They are recoverable harness
state, not irreversible cloud effects.

## Removed Control Logic

The provider and evaluator must not require any of the following:

- delegation at a fixed round;
- compaction at a fixed round;
- investigation, change, restart, or verification at a fixed round;
- phase-specific tool allowlists inferred only from round number;
- a mandatory compaction event in a short cloud incident run;
- a prescribed tool trace used to make a recording appear agentic.

Round and tool counts remain bounded resource budgets, not workflow stages.

## Agent Loop

The control loop has one stable shape:

1. materialize the best current context from durable state;
2. call the model with the tools actually available in the current scope;
3. validate tool arguments, permissions, scope, and approvals;
4. execute allowed tools and append their real results;
5. update Todo, Evidence, Session Memory inputs, and local Project Memory inputs;
6. continue until the model completes or a real bounded stop condition occurs.

The model decides how to investigate, whether to delegate, when evidence is
sufficient, what repair to propose, how to react to failed verification, and
when the incident is complete. The harness may reject unsafe or invalid actions,
but must not replace those decisions with a procedural state machine.

## Shared Model Transport

Normal turns, semantic compaction, memory selection, and memory extraction use
one OpenAI-compatible transport abstraction. It owns:

- base URL, model, API key, TLS CA, and timeout configuration;
- request serialization and response envelope parsing;
- redacted error classification;
- bounded retry with backoff for transient failures;
- explicit non-retryable configuration and certificate failures.

No model-backed subsystem may implement a second independent `urlopen` path.
The verified `certifi` trust store must be applied consistently.

## Context Pipeline

Before every model call, the harness performs the inexpensive deterministic
pipeline in this order:

1. persist or reference oversized tool results;
2. remove or stub old, low-value tool-result bodies without breaking tool pairs;
3. preserve the recent working tail;
4. inject durable Todo, recent Evidence, child reports, Session Memory, and
   relevant Project Memory;
5. estimate the actual model input budget.

Semantic compaction runs only when the materialized input approaches the
configured pressure threshold. A manual compact tool may remain available but
is never required for a normal incident. A `prompt_too_long` response permits
one bounded reactive compaction and retry.

Compaction failure never advances the compact boundary or overwrites the last
valid memory. Transient failures use the shared retry policy. A non-retryable
failure leaves the current context intact and emits a precise, redacted event.

## Session Memory

Session Memory preserves one investigation across compaction and process
restart. Each revision must retain:

- incident objective and user constraints;
- Todo state and remaining work;
- confirmed facts and unresolved questions;
- Evidence IDs rather than full historical output;
- target files and the latest observed full SHA-256 when relevant;
- proposed, pending, approved, applied, verified, failed, or rolled-back changes;
- latest behavioral verification results;
- concrete next actions.

Memory output is strictly validated for run identity, investigation identity,
monotonic transcript coverage, evidence ownership, field bounds, and redaction.
Only a fully valid memory revision and boundary are committed atomically.

## Project Memory

Project Memory is local, automatic, cross-investigation memory. It uses a small
durable catalog plus individual structured Markdown or equivalent records so
the catalog can stay cheap while full entries are loaded on demand.

At terminal investigation completion, a restricted extractor may persist:

- verified stable project facts;
- verified service relationships and diagnostic entry points;
- recurring failure modes supported by owned Evidence;
- repairs and verification procedures that succeeded;
- rollback or recovery lessons that were actually exercised.

It must not persist:

- unverified hypotheses;
- secrets, credentials, raw sensitive logs, or unnecessary host identifiers;
- unsupported model conclusions;
- one-off volatile values presented as stable facts.

Every record includes project ID, applicable services, source investigation ID,
source Evidence IDs, creation and last-confirmed timestamps, and status. Memory
selection loads only a bounded relevant subset. Loaded memories are advisory:
the model must verify them against the current environment before cloud changes.

Extraction and consolidation are local harness operations and require no human
approval. Conflicting or stale entries are superseded or retired without
destroying their provenance.

## Repetition Feedback

The harness may detect an exact or semantically equivalent repeated observation
when its prior result remains valid. It returns a normal tool result explaining
the existing Evidence ID, file hash, or observation and asks the model to either
reuse it or state what external change requires a refresh.

This is feedback, not a phase gate. A first read, a read after hash mismatch, or
a read justified by changed external state remains allowed.

## Acceptance

### Free Agent Loop

Run an incident not encoded in provider prompts. The model must investigate and
adapt without fixed-round rules. Evaluation checks boundedness, evidence
ownership, approvals, mutations, verification, and conclusions—not tool order.

### Context Pressure

Use the real provider and real agent loop with safe, large read-only results.
Trigger pressure-based compaction and prove that objective, constraints, Todo,
Evidence references, relevant hashes, and remaining work survive. Record token
counts before and after and prove the agent completes rather than re-reading the
entire history.

### Cross-Investigation Memory

Complete one verified investigation, automatically extract Project Memory, then
start a fresh investigation. Prove relevant memory is selected, provenance is
visible, current evidence is revalidated, and an unverified hypothesis is not
persisted or loaded as fact.

### Real Cloud Closed Loop

On the Tencent target, complete discovery, exact approvals, minimal changes,
service recovery, full behavioral matrix, one rollback exercise, reapplication,
and final success. Compaction may occur naturally but is not mandatory. Publish
only a redacted trace, report, matrix, manifest, and recording that satisfy the
outcome evaluator.

## Migration and Compatibility

- Existing transcripts, Session Memory revisions, compact boundaries, evidence,
  and changesets remain readable.
- New Project Memory storage is additive and project-scoped.
- Existing fixed-round provider tests and evaluator assertions are replaced by
  state-, pressure-, safety-, and outcome-based tests.
- The refactor is delivered in independently testable commits; the cloud run is
  performed only after local and provider integration tests pass.

## Non-Goals

- training or fine-tuning a model;
- encoding cloud incident playbooks as procedural decision trees;
- automatic unapproved cloud mutation;
- global memory shared across unrelated projects;
- vector search, embeddings, or a remote memory service in the first version;
- rewriting the TUI, report renderer, remote gateway, or changeset engine.
