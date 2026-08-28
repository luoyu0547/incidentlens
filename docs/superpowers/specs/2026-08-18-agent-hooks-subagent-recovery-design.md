# Agent Hooks and SubAgent Recovery Design

## Goal

Add a small Claude Code-style hook mechanism for Agent Harness observability and
close the two material SubAgent gaps: duplicated delegation validation and
non-durable parent result delivery.

## Scope

This change adds fixed internal hook events around existing execution choke
points, centralizes delegation validation, and makes child-report delivery
restart-safe and idempotent.

It does not replace `ToolExecutor`, move permissions into hooks, introduce a new
action framework, add recursive agents, or change the existing child transcript
isolation model.

## Current State

Ordinary model tool requests already follow a strong execution chain:

`ToolRegistry -> ProviderOutputValidator -> ToolExecutor -> Gateway -> policy / approval`

The chain performs schema and scope checks twice, persists tool-call state before
execution, protects approval replay, produces redacted evidence, and classifies
uncertain remote results. These guarantees remain unchanged.

Three areas are incomplete:

1. There is no common hook surface for recording tool, compaction, and SubAgent
   lifecycle events for later evaluation.
2. Structured `child_delegation` and the `delegate_child` tool duplicate package
   construction and do not share one registry-aware validation function.
3. A completed `ChildReport` is held in the live parent's in-memory task list until
   drained. A restart between child completion and parent delivery can lose that
   delivery opportunity.

## Design

### Fixed hook runner

Add a small `HookRunner` with a fixed event enum:

- `PreToolUse`
- `PostToolUse`
- `ToolError`
- `SubAgentStart`
- `SubAgentStop`
- `PreCompact`
- `PostCompact`

Each hook receives an immutable, redacted event payload containing stable IDs,
the action name, timestamps, status, and bounded metadata. Hook results cannot
grant permission, replace arguments, suppress policy checks, or execute a tool.
The initial runtime registers only existing event/audit recording and evaluation
collectors. No plugin discovery or user-script execution is introduced.

`ToolExecutor.execute()` emits tool hooks around its current validation and handler
call. The orchestrator emits compact and SubAgent hooks at their existing entry
and completion points. Hook failure is recorded and ignored for enforcement
purposes: security decisions continue to come exclusively from the existing
registry, guard, gateway policy, and approval services.

### Shared delegation validation

Add one `DelegationValidator` used by both structured delegation and the
`delegate_child` tool before a `DelegatedTaskPackage` is persisted. It validates:

- the parent is not a child run;
- the investigation child budget permits another child;
- project and target match the parent;
- host scope is narrowed within the parent's allowed host paths;
- container scope names a registered service and one of its registered
  containers;
- delegated container paths are a subset of the service's registered container
  paths and any applicable parent restriction;
- seed evidence belongs to the parent;
- every child budget axis is positive and no larger than the parent's remaining
  envelope.

The validator returns a complete `DelegatedTaskPackage`; callers do not construct
partially validated packages themselves. Both existing Provider output forms are
retained to avoid an unrelated model-contract migration.

The investigation-level usage budget and global semaphore remain the final hard
limits. This design does not introduce a transferable token ledger. It only
prevents an individual child request from declaring a wider envelope than its
parent can still supply.

### Durable child-report receipts

Add an append-once `ChildReportReceipt` record keyed uniquely by child run ID. It
stores:

- child and parent run IDs;
- the validated `ChildReport` payload;
- the recorded child-report evidence ID;
- creation time;
- optional delivery time.

When a child reaches a terminal state, the runtime builds its report, records the
child-report evidence, and commits the receipt before returning from the child
task. Repeating this operation for the same child returns the existing receipt.

The parent checks for undelivered receipts:

- at the beginning of each loop step;
- while draining or waiting for children;
- when a run is resumed after process recovery.

Delivery appends the bounded notification to the parent transcript, attaches the
existing evidence reference, updates usage once, and marks the receipt delivered
in one store transaction. If the transaction fails, the receipt remains
undelivered and can be retried. Child transcript messages are never copied to the
parent.

### Recovery behavior

On startup, existing recovery still classifies in-flight tools and run states.
When a parent or terminal child is resumed, receipt reconciliation repairs the
specific gap between child completion and parent consumption. It does not rerun a
terminal child and does not synthesize a report from an incomplete child.

## Interfaces and Files

- Create `investigation/hooks.py`: event enum, immutable payload, and `HookRunner`.
- Create `investigation/delegation.py`: shared `DelegationValidator` and package
  construction.
- Modify `investigation/tool_executor.py`: call the shared validator and emit tool
  hooks without changing permission enforcement.
- Modify `investigation/orchestrator.py`: use shared delegation validation, emit
  compact/SubAgent hooks, and reconcile receipts.
- Modify `investigation/types.py`: add `ChildReportReceipt`.
- Modify `investigation/store.py`: migrate, create, list-undelivered, and atomically
  deliver receipts.
- Modify `runtime.py`: construct one HookRunner and one DelegationValidator and
  inject them into the existing services.
- Extend the existing tool, orchestrator, store, and recovery tests.

## Verification

The implementation is complete when tests prove:

1. every executed, rejected, approval-waiting, and failed tool emits a paired hook
   lifecycle without changing its policy result;
2. a failing hook cannot allow a forbidden tool or prevent mandatory approval;
3. both delegation input forms accept and reject the same scope and evidence;
4. an unregistered service, container, or delegated path is rejected before a
   child session is created;
5. child runs cannot delegate grandchildren;
6. a terminal child creates exactly one receipt and one child-report evidence
   record;
7. a simulated restart before parent delivery results in exactly one notification,
   evidence attachment, and usage increment;
8. repeated recovery is idempotent and does not rerun a terminal child.

## Non-Goals

- Dynamic hook plugins, configuration files, or arbitrary hook scripts.
- Moving command policy or approval decisions into hooks.
- A generalized action/pipeline abstraction.
- Recursive SubAgents, agent teams, worktrees, or background task scheduling.
- Copying child transcripts into the parent context.
