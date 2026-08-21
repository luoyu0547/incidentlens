# Terminal Run and Recording Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user launch, supervise, approve, repair and record a complete investigation from one colored terminal session.

**Architecture:** A new `run` command creates and starts an investigation inside the CLI process. A sequence-aware event presenter incrementally updates Textual widgets and fans the same live events to JSONL, ANSI transcript and plain-text recorders.

**Tech Stack:** Python 3.12, Textual, Rich, asyncio, JSONL, pytest Textual pilot.

**Spec:** `docs/superpowers/specs/2026-08-21-hard-cloud-incident-terminal-design.md`

## Global Constraints

- Depends on `2026-08-21-runtime-identity-reacquisition.md`.
- Recording begins before investigation creation and is never reconstructed from SQLite.
- No hidden reasoning, raw secrets, private keys or unredacted command output may be rendered or recorded.
- Color is semantic enhancement; `NO_COLOR=1` remains fully readable.

---

### Task 1: One-command run request

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/run_request.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/cli/app.py`
- Test: `tests/cli/test_run_command.py`

**Interfaces:**
- Produces: `RunCommand(project_id, target_id, service, scope, symptom, record_path)` and `create_and_start(runtime, command) -> Investigation`.

- [ ] **Step 1: Add failing argument and lifecycle tests**

```python
def test_run_command_accepts_registered_identity_not_ssh_credentials():
    args = parse_args(["run", "--project", "p", "--target", "t", "--service", "api", "symptom"])
    assert args.project == "p"
    assert not hasattr(args, "private_key")
```

Test unknown project/target/service, host scope derivation, and that the app is mounted before the background Agent starts so initial events are visible.

- [ ] **Step 2: Run and verify `run` is unknown**

Run: `uv run pytest tests/cli/test_run_command.py -v`

Expected: FAIL because the parser supports only `investigate` and `report`.

- [ ] **Step 3: Implement parser and lifecycle service**

```python
subparser = subparsers.add_parser("run")
subparser.add_argument("symptom")
subparser.add_argument("--project", required=True)
subparser.add_argument("--target", required=True)
subparser.add_argument("--service", required=True)
subparser.add_argument("--scope", choices=("host", "container"), default="host")
subparser.add_argument("--record", type=Path)
```

Resolve Scope exclusively from registry records. Start the orchestrator via a Textual worker after event subscription is ready.

- [ ] **Step 4: Run CLI lifecycle tests**

Run: `uv run pytest tests/cli/test_run_command.py tests/cli/test_screens.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/cli tests/cli
git commit -m "feat(cli): launch an investigation from one command"
```

### Task 2: Semantic event cards and live updates

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/presentation.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/widgets/event_card.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/cli/screens/investigation.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/cli/app.py`
- Test: `tests/cli/test_presentation.py`
- Test: `tests/cli/test_live_screen.py`

**Interfaces:**
- Consumes: ordered `RuntimeEvent` objects.
- Produces: `PresentedEvent(key, label, symbol, color, title, lines, terminal)` and in-place card updates by stable key.

- [ ] **Step 1: Write failing semantic mapping tests**

```python
@pytest.mark.parametrize((kind, symbol, color), [
    ("agent_round.started", "◆", "#58a6ff"),
    ("context.compacted", "⇣", "#bc8cff"),
    ("approval.requested", "⏸", "#d29922"),
    ("investigation.completed", "■", "#3fb950"),
])
def test_event_semantics(kind, symbol, color):
    event = runtime_event(kind)
    presented = present_event(event, no_color=False)
    assert presented.symbol == symbol
    assert presented.color == color
```

- [ ] **Step 2: Run tests and confirm no presenter exists**

Run: `uv run pytest tests/cli/test_presentation.py tests/cli/test_live_screen.py -v`

Expected: FAIL.

- [ ] **Step 3: Implement the presenter and focused widgets**

Use a data-driven style table. Observation cards show redacted target, arguments, duration, preview and evidence count. Action cards show diff/impact/verification/rollback and approval. Parent/child events render as an indented tree.

- [ ] **Step 4: Replace periodic clear/repaint with sequence consumption**

Subscribe to `runtime.broker`; preload only events before the subscription sequence, then append new events exactly once. Update a running card by its tool/change/approval ID rather than clearing the activity log.

- [ ] **Step 5: Test color and `NO_COLOR` snapshots**

Run: `NO_COLOR=1 uv run pytest tests/cli/test_presentation.py tests/cli/test_live_screen.py -q`

Expected: PASS with symbols and labels preserved.

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/cli tests/cli
git commit -m "feat(cli): render live semantic agent events"
```

### Task 3: Approval, rollback and report commands

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/cli/screens/investigation.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/routes/changes.py`
- Test: `tests/cli/test_live_screen.py`

**Interfaces:**
- Produces terminal commands `:approve <id>`, `:reject <id>`, `:rollback <changeset-id>`, `:report`.

- [ ] **Step 1: Add failing command tests**

Verify exact approval display, invalid IDs, rollback approval creation, rejection, and continuation of the parked run after a decision.

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/cli/test_live_screen.py -k 'approve or rollback' -v`

Expected: FAIL because rollback is not a CLI command.

- [ ] **Step 3: Implement commands using existing services**

Never call ChangeManager internals from the widget. Add a focused application service method returning the exact approval ID/status, then render the durable events it emits.

- [ ] **Step 4: Run CLI, approval and changeset tests**

Run: `uv run pytest tests/cli tests/approvals tests/changes -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/cli apps/control-plane/src/incidentlens_control_plane/routes tests
git commit -m "feat(cli): supervise approvals and rollback in session"
```

### Task 4: Live terminal recording

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/recording.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/cli/app.py`
- Test: `tests/cli/test_recording.py`

**Interfaces:**
- Produces: `SessionRecorder.start()`, `record_event(event, presented)`, `record_input(command)`, `close(report_paths)`.

- [ ] **Step 1: Add failing synchronous-recording tests**

```python
assert cast_lines[0][0] == 0
assert json.loads(trace_lines[0])["kind"] == "session.started"
assert "API_KEY" not in all_outputs
assert "◆ MODEL" in plain_text
```

- [ ] **Step 2: Run tests and verify recorder is absent**

Run: `uv run pytest tests/cli/test_recording.py -v`

Expected: FAIL.

- [ ] **Step 3: Implement append-time fan-out**

Write asciinema v2 header/events, JSONL trace and ANSI-free text as each event/input is presented. Flush every record; redact via the same presenter payload. Write `session.finished` only after reports exist.

- [ ] **Step 4: Test crash/interrupt closure and full CLI suite**

Run: `uv run pytest tests/cli -q`

Expected: PASS; interrupted recordings remain parseable and end with `session.interrupted`.

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/cli tests/cli
git commit -m "feat(cli): record live investigation terminal sessions"
```
