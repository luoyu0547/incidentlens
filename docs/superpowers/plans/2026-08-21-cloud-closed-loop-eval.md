# Cloud Closed-Loop Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy only the dual-regression target to the cloud, record one real terminal-led investigation, and machine-check every closed-loop invariant.

**Architecture:** A provisioning script copies only `infra/acceptance`, starts the cloud-bound Compose files and verifies the pre-repair matrix. A local evaluator consumes live trace/report/change artifacts—not SQLite—to decide pass/fail.

**Tech Stack:** POSIX shell, SSH, Docker Compose, Python 3.12, JSONL, pytest.

**Spec:** `docs/superpowers/specs/2026-08-21-hard-cloud-incident-terminal-design.md`

## Global Constraints

- Depends on all three earlier plans.
- Remote host contains no IncidentLens source, model key or Provider configuration.
- The evaluator never supplies expected root-cause text to the Agent.
- A failed/partial run is reported as failed; Docker Hub publication remains out of scope.

---

### Task 1: Safe target-only provisioning

**Files:**
- Create: `scripts/cloud_acceptance_target.sh`
- Test: `tests/acceptance/test_cloud_script.py`

**Interfaces:**
- CLI: `cloud_acceptance_target.sh provision|status|verify-precondition|stop --host <ssh-alias>`.

- [ ] **Step 1: Add failing shell-contract tests**

Assert dry-run paths are exactly `/opt/incidentlens-target`, copied sources are under `infra/acceptance`, ports bind to loopback, and the script refuses `/`, `~`, empty hosts and unresolved targets.

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/acceptance/test_cloud_script.py -v`

Expected: FAIL because the script is absent.

- [ ] **Step 3: Implement idempotent provisioning**

Use explicit `rsync` include roots, remote `install -d`, Docker installation detection, Compose build/up and precondition matrix. Never copy `.env`, `.git`, runtime databases or repository root.

- [ ] **Step 4: Run static and dry-run tests**

Run: `shellcheck scripts/cloud_acceptance_target.sh && uv run pytest tests/acceptance/test_cloud_script.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/cloud_acceptance_target.sh tests/acceptance/test_cloud_script.py
git commit -m "test(cloud): provision target-only acceptance services"
```

### Task 2: Trace-based harness evaluator

**Files:**
- Create: `tests/eval/cloud_closed_loop.py`
- Create: `tests/eval/test_cloud_closed_loop.py`
- Modify: `tests/eval/types.py`

**Interfaces:**
- Produces: `evaluate_cloud_closed_loop(trace_path, report_path, matrix_path) -> EvaluationResult`.

- [ ] **Step 1: Add failing invariant tests with minimal trace fixtures**

```python
assert result.metrics.root_causes == 2
assert result.metrics.remote_observations_after_compact >= 1
assert result.metrics.unapproved_mutations == 0
assert result.metrics.subagents >= 1
assert result.metrics.rollback_reproduced_fault is True
assert result.metrics.final_matrix_passed is True
```

Also make every forbidden condition from Spec section 10 produce a named failure.

- [ ] **Step 2: Run evaluator tests**

Run: `uv run pytest tests/eval/test_cloud_closed_loop.py -v`

Expected: FAIL because evaluator/types are absent.

- [ ] **Step 3: Implement ordered trace evaluation**

Correlate events by internal run/tool/change/approval IDs. Count only observations after the compact event for reacquisition. Require approval to precede mutation, rollback failure reproduction to precede reapply, and the final four cells to be 201.

- [ ] **Step 4: Run all deterministic eval tests**

Run: `uv run pytest tests/eval -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/eval
git commit -m "test(eval): enforce cloud closed-loop invariants"
```

### Task 3: Execute and preserve the real terminal run

**Files:**
- Create: `docs/cloud-acceptance/hard-incident/README.md`
- Create: `docs/cloud-acceptance/hard-incident/manifest.json`
- Create: `docs/cloud-acceptance/hard-incident/final-matrix.json`
- Create: `docs/assets/hard-incident.cast`
- Create: `docs/assets/hard-incident.txt`
- Create: `docs/assets/hard-incident.trace.jsonl`
- Create: `docs/assets/hard-incident-report.md`
- Create: `docs/assets/hard-incident-report.html`

**Interfaces:**
- Consumes the provisioned SSH alias, local `.env` Provider settings and `incidentlens run`.
- Produces immutable redacted public artifacts plus hashes in `manifest.json`.

- [ ] **Step 1: Provision and verify only the cloud target**

Run: `scripts/cloud_acceptance_target.sh provision --host incidentlens-tencent`

Expected: pre-repair matrix shows stable/normal 201, stable/high failure, canary/normal failure, canary/high failure; remote `/opt/incidentlens` is absent.

- [ ] **Step 2: Start the recorded terminal investigation**

Run:

```bash
INCIDENTLENS_DATA_DIR="$PWD/artifacts/hard-cloud-runtime" \
uv run --env-file .env incidentlens run \
  --project tencent-cloud-acceptance \
  --target tencent-cvm \
  --service api-gateway \
  --scope host \
  --record "$PWD/artifacts/hard-incident.cast" \
  "订单接口间歇性失败：部分请求数据库错误，部分高金额订单支付失败。请自主调查，在批准后修复并验证。"
```

Expected: the operator supplies approvals only through the displayed TUI; no API or SQLite side channel is used.

- [ ] **Step 3: Run the evaluator before publishing artifacts**

Run: `uv run python tests/eval/cloud_closed_loop.py --trace artifacts/hard-incident.trace.jsonl --reports-dir artifacts/hard-cloud-runtime/reports --matrix artifacts/hard-incident-final-matrix.json`

Expected: PASS for every Spec section 9 invariant. The evaluator reads the investigation ID from the live trace, selects the matching report, and records that exact ID in `manifest.json`.

- [ ] **Step 4: Redact, hash and copy the exact live artifacts**

Use the repository redactor, then verify no API key, private key, raw host IP or credential is present. `manifest.json` records SHA-256, run/investigation IDs, model ID, UTC timestamps, remote target label and evaluator version.

- [ ] **Step 5: Verify replay and public report**

Run: `asciinema cat docs/assets/hard-incident.cast && uv run python tests/eval/cloud_closed_loop.py --trace docs/assets/hard-incident.trace.jsonl --report docs/assets/hard-incident-report.md --matrix docs/cloud-acceptance/hard-incident/final-matrix.json`

Expected: replay is readable and evaluator passes from published artifacts alone.

- [ ] **Step 6: Commit**

```bash
git add docs/cloud-acceptance/hard-incident docs/assets/hard-incident* 
git commit -m "docs: publish real hard cloud incident run"
```

### Task 4: Final regression and honest project documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/phase-4-agent-runtime-verification.md`

- [ ] **Step 1: Run static checks and the complete suite**

Run: `uv run ruff check . && uv run pytest -q`

Expected: PASS except explicitly documented opt-in tests that are skipped without their environment flags.

- [ ] **Step 2: Re-run cloud evaluator and target status**

Run: `scripts/cloud_acceptance_target.sh status --host incidentlens-tencent && uv run python tests/eval/cloud_closed_loop.py --trace docs/assets/hard-incident.trace.jsonl --report docs/assets/hard-incident-report.md --matrix docs/cloud-acceptance/hard-incident/final-matrix.json`

Expected: target services healthy, final matrix 201 in all four cells, evaluator PASS, IncidentLens source absent remotely.

- [ ] **Step 3: Document commands, evidence and limitations**

README links the recording/report, distinguishes deterministic CI from the single real cloud run, states that the target was controlled and fault-injected, and does not claim production readiness or Docker Hub availability.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/phase-4-agent-runtime-verification.md
git commit -m "docs: document cloud closed-loop acceptance"
```
