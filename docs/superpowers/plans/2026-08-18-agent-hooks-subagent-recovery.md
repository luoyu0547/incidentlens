# Agent Hooks and SubAgent Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal fixed Hook mechanism and make both delegation validation and child-report delivery consistent, durable, and restart-safe.

**Architecture:** Hooks are immutable lifecycle notifications and never participate in permission decisions. A shared `DelegationValidator` produces the only valid delegated package for both Provider output paths, while an append-once receipt lets parents consume terminal child reports exactly once across restarts.

**Tech Stack:** Python 3.12, asyncio, Pydantic, SQLite transactions, pytest.

**Spec:** `docs/superpowers/specs/2026-08-18-agent-hooks-subagent-recovery-design.md`

## Global Constraints

- Keep `ToolRegistry -> ProviderOutputValidator -> ToolExecutor -> Gateway -> policy / approval` as the binding permission path.
- Hooks cannot grant permission, rewrite tool arguments, suppress approval, or execute actions.
- Do not add dynamic hook discovery, arbitrary scripts, plugins, or a generalized action pipeline.
- Keep both current delegation output forms and make them share validation.
- Never copy child transcripts into a parent context.
- Child-report delivery must be idempotent after any number of recovery attempts.

---

### Task 1: Add the fixed HookRunner and tool lifecycle events

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/hooks.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/events/types.py:8`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/tool_executor.py:276`
- Test: `tests/investigation/test_hooks.py`
- Modify: `tests/investigation/test_tool_executor.py`

**Interfaces:**
- Produces: `HookEventType`, frozen `HookEvent`, `HookRunner.register(event_type, callback)`, async `HookRunner.emit(event)`, and `RuntimeHookRecorder` that writes `RuntimeEventType.AGENT_HOOK`.
- Consumes: `ToolRequest`, `AgentRun`, `ToolOutcome`, and redacted bounded error strings.

- [ ] **Step 1: Write HookRunner ordering and failure-isolation tests**

```python
@pytest.mark.asyncio
async def test_hook_runner_calls_registered_callbacks_in_order() -> None:
    seen: list[str] = []
    runner = HookRunner()
    runner.register(HookEventType.PRE_TOOL_USE, lambda event: seen.append("first"))
    runner.register(HookEventType.PRE_TOOL_USE, lambda event: seen.append("second"))
    failures = await runner.emit(hook_event(HookEventType.PRE_TOOL_USE))
    assert seen == ["first", "second"]
    assert failures == ()


@pytest.mark.asyncio
async def test_hook_failure_is_returned_not_raised() -> None:
    runner = HookRunner()
    runner.register(HookEventType.PRE_TOOL_USE, raising_callback)
    failures = await runner.emit(hook_event(HookEventType.PRE_TOOL_USE))
    assert len(failures) == 1
    assert "secret" not in failures[0]
```

- [ ] **Step 2: Run the new tests and verify import failure**

Run: `uv run pytest tests/investigation/test_hooks.py -q`

Expected: FAIL because `investigation/hooks.py` does not exist.

- [ ] **Step 3: Implement the minimal fixed runner**

```python
class HookEventType(StrEnum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    TOOL_ERROR = "ToolError"
    SUBAGENT_START = "SubAgentStart"
    SUBAGENT_STOP = "SubAgentStop"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"


class HookEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_type: HookEventType
    agent_run_id: str
    action_name: str
    occurred_at: datetime
    status: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class HookRunner:
    async def emit(self, event: HookEvent) -> tuple[str, ...]:
        failures: list[str] = []
        for callback in self._callbacks[event.event_type]:
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                failures.append(redact_message(str(exc), max_length=500).message_redacted)
        return tuple(failures)


class RuntimeHookRecorder:
    def __init__(self, publisher: InvestigationEventPublisher) -> None:
        self._publisher = publisher

    def __call__(self, event: HookEvent) -> None:
        self._publisher.emit(
            RuntimeEventType.AGENT_HOOK,
            occurred_at=event.occurred_at,
            hook_type=event.event_type.value,
            agent_run_id=event.agent_run_id,
            action_name=event.action_name,
            status=event.status,
            metadata=event.metadata,
        )
```

Add `AGENT_HOOK = "agent_hook"` to `RuntimeEventType`. The recorder is the one
default runtime callback, giving Phase C a durable trace without adding a second
event store.

- [ ] **Step 4: Emit paired tool events without moving enforcement**

Inject `hooks: HookRunner | None = None` into `ToolExecutor`. Emit `PreToolUse`
at the beginning of `execute()`, `PostToolUse` for every returned outcome, and
`ToolError` when execution maps an exception to failed/uncertain output. Include
only IDs, tool name, final status, output byte count, and approval ID; do not put
raw arguments or output into hook metadata.

```python
await self._hooks.emit(
    HookEvent(
        event_type=HookEventType.PRE_TOOL_USE,
        agent_run_id=run.agent_run_id,
        action_name=request.tool_name,
        occurred_at=now,
        metadata={"tool_call_id": request.tool_call_id},
    )
)
```

- [ ] **Step 5: Prove hooks cannot bypass policy and commit**

```python
@pytest.mark.asyncio
async def test_failing_pre_tool_hook_cannot_allow_forbidden_request(harness) -> None:
    harness.hooks.register(HookEventType.PRE_TOOL_USE, raising_callback)
    outcome = await harness.executor.execute(forbidden_request(), harness.run)
    assert outcome.status is ToolCallStatus.FAILED
    assert harness.transport.exec_calls == 0
```

Run: `uv run pytest tests/investigation/test_hooks.py tests/investigation/test_tool_executor.py -q`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation/hooks.py apps/control-plane/src/incidentlens_control_plane/events/types.py apps/control-plane/src/incidentlens_control_plane/investigation/tool_executor.py tests/investigation/test_hooks.py tests/investigation/test_tool_executor.py
git commit -m "feat(agent): add fixed harness hooks"
```

### Task 2: Centralize delegation validation for both input forms

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/delegation.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/tool_executor.py:767`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py:1427`
- Test: `tests/investigation/test_delegation.py`
- Modify: `tests/investigation/test_orchestrator.py`

**Interfaces:**
- Produces: `DelegationSpec` and `DelegationValidator.prepare(parent, investigation, spec) -> DelegatedTaskPackage`.
- Consumes: `ProjectRegistryStore`, `InvestigationGuard`, parent evidence, current investigation usage, and requested/default child budget.

- [ ] **Step 1: Write table-driven shared-boundary tests**

```python
@pytest.mark.parametrize("source", ["structured", "tool"])
def test_unregistered_container_is_rejected(source, validator, parent, investigation) -> None:
    spec = delegation_spec(source, container_name="not-registered")
    with pytest.raises(DelegationRejected, match="registered container"):
        validator.prepare(parent, investigation, spec)


@pytest.mark.parametrize("source", ["structured", "tool"])
def test_container_path_outside_registry_is_rejected(source, validator, parent, investigation):
    spec = delegation_spec(source, allowed_container_paths=(PurePosixPath("/etc"),))
    with pytest.raises(DelegationRejected, match="container paths"):
        validator.prepare(parent, investigation, spec)
```

Add cases for child parent, project/target mismatch, foreign evidence, exhausted
child budget, and each child budget axis exceeding the parent envelope.

- [ ] **Step 2: Run tests and verify the new module is missing**

Run: `uv run pytest tests/investigation/test_delegation.py -q`

Expected: FAIL because `DelegationValidator` is undefined.

- [ ] **Step 3: Implement normalized specs and registry-aware validation**

```python
class DelegationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    child_run_id: str
    task_prompt: str
    scope: AgentScope
    evidence_ids: tuple[str, ...] = ()
    budget: AgentBudget | None = None


class DelegationValidator:
    def prepare(
        self,
        parent: AgentRun,
        investigation: Investigation,
        spec: DelegationSpec,
    ) -> DelegatedTaskPackage:
        allowed, reason = self._guard.can_spawn_child(parent, investigation)
        if not allowed:
            raise DelegationRejected(reason)
        self._validate_registered_scope(parent, spec.scope)
        self._validate_evidence(parent, spec.evidence_ids)
        budget = self._bounded_budget(parent, spec.budget)
        return DelegatedTaskPackage(
            child_run_id=spec.child_run_id,
            parent_run_id=parent.agent_run_id,
            investigation_id=parent.investigation_id,
            task_prompt=spec.task_prompt,
            scope=spec.scope,
            budget=budget,
            evidence_ids=spec.evidence_ids,
        )
```

Resolve the project service by `compose_service`, require the delegated container
in `container_names`, and use path containment rules consistent with existing
ToolExecutor path validation. Do not accept an empty registered-path set as an
implicit widening for container children.

- [ ] **Step 4: Route both callers through `prepare()`**

The tool handler converts its JSON arguments into `DelegationSpec`; the structured
Provider path converts `ChildDelegationRequest` into the same type. Delete their
duplicated `_scope_within`, owned-evidence, and package-construction branches only
after the shared tests pass. Keep persistence, usage accounting, and child spawn
in their existing caller-specific locations.

```python
spec = DelegationSpec(
    child_run_id=delegation.child_run_id,
    task_prompt=delegation.task_prompt,
    scope=delegation.scope,
    evidence_ids=delegation.evidence_ids,
    budget=requested_budget,
)
package = self._delegation.prepare(run, investigation, spec)
```

- [ ] **Step 5: Run delegation/orchestrator tests and commit**

Run: `uv run pytest tests/investigation/test_delegation.py tests/investigation/test_tool_executor.py tests/investigation/test_orchestrator.py -q`

Expected: PASS with both delegation forms producing equivalent packages and
rejections.

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation/delegation.py apps/control-plane/src/incidentlens_control_plane/investigation/tool_executor.py apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py tests/investigation/test_delegation.py tests/investigation/test_tool_executor.py tests/investigation/test_orchestrator.py
git commit -m "fix(agent): unify child delegation validation"
```

### Task 3: Persist append-once child report receipts

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/types.py:248`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/store.py:315`
- Test: `tests/investigation/test_store.py`

**Interfaces:**
- Produces: `ChildReportReceipt`, `put_child_report_receipt()`, `get_child_report_receipt()`, `list_undelivered_child_report_receipts()`, and transactional `deliver_child_report_receipt()`.
- Consumes: validated `ChildReport`, deduplicated child-report evidence ID, updated parent run/investigation, and one parent transcript notification.

- [ ] **Step 1: Write receipt persistence and uniqueness tests**

```python
def test_child_report_receipt_is_append_once(store) -> None:
    receipt = child_receipt("child-1")
    assert store.put_child_report_receipt(receipt) == receipt
    assert store.put_child_report_receipt(receipt) == receipt
    assert store.list_undelivered_child_report_receipts("parent-1") == (receipt,)


def test_conflicting_receipt_for_same_child_is_rejected(store) -> None:
    store.put_child_report_receipt(child_receipt("child-1"))
    with pytest.raises(ChildReportReceiptConflict):
        store.put_child_report_receipt(child_receipt("child-1", evidence_id="ev-other"))
```

- [ ] **Step 2: Write atomic delivery rollback test**

```python
def test_receipt_delivery_rolls_back_all_parent_updates_on_conflict(store) -> None:
    receipt = seed_receipt(store)
    notification = existing_sequence_notification()
    with pytest.raises(TranscriptConflict):
        store.deliver_child_report_receipt(
            receipt.child_run_id,
            parent=updated_parent(),
            investigation=updated_investigation(),
            notification=notification,
            delivered_at=NOW,
        )
    assert store.get_child_report_receipt(receipt.child_run_id).delivered_at is None
    assert store.get_agent_run("parent-1").evidence == ()
```

- [ ] **Step 3: Run tests and verify schema/type failures**

Run: `uv run pytest tests/investigation/test_store.py -q`

Expected: FAIL because receipt types, table, and store methods do not exist.

- [ ] **Step 4: Add the strict type, table, and store methods**

```python
class ChildReportReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    child_run_id: str
    parent_run_id: str
    report: ChildReport
    evidence_id: str
    created_at: datetime
    delivered_at: datetime | None = None
```

Create `child_report_receipts` with `child_run_id` as primary key, indexed by
`(parent_run_id, delivered_at)`. `put_child_report_receipt()` returns an identical
existing record but rejects conflicting content. `deliver_child_report_receipt()`
uses one SQLite transaction to insert the notification, update parent and
investigation JSON/usage columns, and set `delivered_at`; an already delivered
receipt returns unchanged without a second write.

```sql
CREATE TABLE IF NOT EXISTS child_report_receipts (
    child_run_id TEXT PRIMARY KEY,
    parent_run_id TEXT NOT NULL,
    delivered_at TEXT,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_child_report_receipts_delivery
    ON child_report_receipts(parent_run_id, delivered_at);
```

```python
def deliver_child_report_receipt(
    self,
    child_run_id: str,
    *,
    parent: AgentRun,
    investigation: Investigation,
    notification: TranscriptMessage,
    delivered_at: datetime,
) -> ChildReportReceipt:
    """Atomically attach, notify, account, and mark one receipt delivered."""
```

- [ ] **Step 5: Run store tests and commit**

Run: `uv run pytest tests/investigation/test_store.py -q`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation/types.py apps/control-plane/src/incidentlens_control_plane/investigation/store.py tests/investigation/test_store.py
git commit -m "feat(agent): persist child report receipts"
```

### Task 4: Produce and reconcile receipts in the orchestrator

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py:1558`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/events.py:278`
- Test: `tests/investigation/test_orchestrator.py`
- Test: `tests/investigation/test_hooks.py`

**Interfaces:**
- Consumes: Task 1 `HookRunner`, Task 3 receipt store methods, existing evidence dedupe, and child-report construction.
- Produces: `_ensure_child_report_receipt()`, `_deliver_pending_child_reports()`, and SubAgent/compact hook emission.

- [ ] **Step 1: Write restart and exactly-once orchestrator tests**

```python
@pytest.mark.asyncio
async def test_parent_delivers_terminal_child_receipt_after_restart(harness) -> None:
    seed_terminal_child_and_undelivered_receipt(harness)
    first = build_orchestrator(harness, fresh_process=True)
    await first.run("parent-1")
    second = build_orchestrator(harness, fresh_process=True)
    await second.run("parent-1")
    parent = harness.investigations.get_agent_run("parent-1")
    assert [ref.operation_id for ref in parent.evidence].count("child:child-1") == 1
    assert count_child_notifications(harness, "child-1") == 1
    assert harness.investigations.get_child_report_receipt("child-1").delivered_at
```

- [ ] **Step 2: Write SubAgent and compact hook tests**

```python
@pytest.mark.asyncio
async def test_child_and_compact_emit_fixed_hooks(runtime) -> None:
    await run_child_then_manual_compact(runtime)
    assert hook_types(runtime.hooks) == [
        HookEventType.SUBAGENT_START,
        HookEventType.SUBAGENT_STOP,
        HookEventType.PRE_COMPACT,
        HookEventType.POST_COMPACT,
    ]
```

- [ ] **Step 3: Run tests and verify receipts are not reconciled**

Run: `uv run pytest tests/investigation/test_orchestrator.py tests/investigation/test_hooks.py -q`

Expected: FAIL because the orchestrator still returns reports only through
in-memory asyncio task results.

- [ ] **Step 4: Persist before returning and reconcile before each model turn**

Refactor `_run_child()` to return the stored receipt. `_ensure_child_report_receipt()`
must reuse the evidence store's dedupe behavior and then call
`put_child_report_receipt()`. At the start of `_loop_step()` and in child drain
paths, call `_deliver_pending_child_reports()` before building the next Provider
request.

For each receipt, reload parent/investigation, calculate the evidence and usage
delta once, create the next transcript notification sequence, then call the
transactional store delivery method. Append the receipt's `ChildReport` to the
current bounded `child_reports` list only after delivery succeeds.

```python
for receipt in self._store.list_undelivered_child_report_receipts(run.agent_run_id):
    parent, investigation, notification = self._prepare_receipt_delivery(receipt, now)
    delivered = self._store.deliver_child_report_receipt(
        receipt.child_run_id,
        parent=parent,
        investigation=investigation,
        notification=notification,
        delivered_at=now,
    )
    child_reports.append(delivered.report)
```

Emit `SubAgentStart/Stop` around `_run_child()` and `PreCompact/PostCompact`
around both reactive and manual compact paths. A hook failure must not change the
receipt, compact, or run status.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/investigation/test_orchestrator.py tests/investigation/test_hooks.py tests/investigation/test_store.py -q`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py apps/control-plane/src/incidentlens_control_plane/investigation/events.py tests/investigation/test_orchestrator.py tests/investigation/test_hooks.py
git commit -m "feat(agent): recover child reports exactly once"
```

### Task 5: Wire hooks/delegation into runtime and verify startup recovery

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py:140`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/recovery.py`
- Modify: `tests/investigation/test_recovery.py`
- Modify: `tests/test_app.py`
- Modify: `docs/agent-memory-context-design.md`

**Interfaces:**
- Consumes: `HookRunner`, `DelegationValidator`, receipt reconciliation, existing recovery ordering.
- Produces: one shared runtime HookRunner and validator, plus startup reconciliation for terminal child receipts.

- [ ] **Step 1: Add runtime composition and recovery tests**

```python
def test_runtime_shares_hook_runner_and_delegation_validator(runtime) -> None:
    orchestrator = runtime.investigations._orchestrator
    executor = runtime.investigations._executor
    assert orchestrator._context is runtime.context_manager
    assert orchestrator._hooks is executor._hooks
    assert orchestrator._delegation is executor._delegation


@pytest.mark.asyncio
async def test_startup_reconciles_undelivered_terminal_child_once(harness) -> None:
    seed_terminal_child_and_undelivered_receipt(harness)
    await harness.recovery.recover_startup()
    await harness.recovery.recover_startup()
    assert count_child_notifications(harness, "child-1") == 1
```

- [ ] **Step 2: Run focused tests and verify missing wiring**

Run: `uv run pytest tests/test_app.py tests/investigation/test_recovery.py -q`

Expected: FAIL because runtime does not construct the shared services and startup
recovery does not reconcile receipts.

- [ ] **Step 3: Wire the shared services and recovery call**

```python
hooks = HookRunner()
hook_recorder = RuntimeHookRecorder(InvestigationEventPublisher(events, broker))
for event_type in HookEventType:
    hooks.register(event_type, hook_recorder)
delegation = DelegationValidator(projects=projects)
executor = ToolExecutor(
    projects=projects,
    sessions=sessions,
    gateway=remote_tools,
    logs=logs,
    log_store=log_store,
    evidence=evidence_service,
    evidence_store=evidence,
    investigations=investigation_store,
    approvals=approvals,
    hooks=hooks,
    delegation=delegation,
)
orchestrator = AgentOrchestrator(
    store=investigation_store,
    provider=provider,
    executor=executor,
    evidence=evidence_service,
    projects=projects,
    sessions=sessions,
    hooks=hooks,
    delegation=delegation,
    context_manager=context_manager,
)
```

During startup recovery, after run/tool classification and before resuming parent
runs, ask the orchestrator to reconcile receipts for parents that own terminal
children. Keep the existing approval and uncertain-call recovery order intact.

- [ ] **Step 4: Update the context/Harness documentation**

Document fixed Hook events, their non-authoritative security role, the shared
delegation validator, and restart-safe exactly-once child result delivery. State
explicitly that permissions remain in the existing registry/gateway path.

- [ ] **Step 5: Run Phase B verification and commit**

Run: `uv run pytest tests/investigation/test_hooks.py tests/investigation/test_delegation.py tests/investigation/test_tool_executor.py tests/investigation/test_store.py tests/investigation/test_orchestrator.py tests/investigation/test_recovery.py tests/test_app.py -q`

Run: `uv run ruff check apps/control-plane/src/incidentlens_control_plane/investigation apps/control-plane/src/incidentlens_control_plane/runtime.py tests/investigation tests/test_app.py`

Expected: all tests and lint checks PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/runtime.py apps/control-plane/src/incidentlens_control_plane/investigation/recovery.py tests/investigation/test_recovery.py tests/test_app.py docs/agent-memory-context-design.md
git commit -m "test(agent): verify hooks and child recovery"
```
