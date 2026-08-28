# Hard Cloud Incident Closed-Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run one real, terminal-led cloud investigation that finds two independent deployment regressions, obtains approval, repairs both, verifies them, performs a rollback drill, and publishes an auditable recording.

**Architecture:** Implement the work as one delivery with four ordered phases: runtime correctness, terminal UX/recording, deterministic dual-regression target, and cloud evaluation. The phases have focused test gates but produce one final user-visible artifact.

**Tech Stack:** Python 3.12, Pydantic 2, SQLite runtime state, Textual/Rich TUI, OpenAI-compatible Chat Completions, AsyncSSH, Docker Compose, PostgreSQL, pytest, JSONL and asciinema v2 recording.

**Spec:** `docs/superpowers/specs/2026-08-21-hard-cloud-incident-terminal-design.md`

## Global Constraints

- IncidentLens runs locally; the cloud host contains only the target Docker services.
- Provider-generated tool IDs are run-local correlations, never global database identities.
- Reproducible observations are reacquired with remote tools after compaction; SQLite is not an Agent tool.
- Dangerous actions require exact approval, backup, verification and rollback controls.
- No hidden reasoning, credentials, private keys, raw host IPs or unredacted command output enters the recording.
- A failed or partial run is reported as failed; no production-readiness or Docker Hub claim is made.

---

## Phase 1: Runtime correctness and evidence semantics

### Task 1: Namespace tool-call identity

**Files:** `investigation/types.py`, `investigation/orchestrator.py`, `investigation/store.py`, `investigation/tool_executor.py`, corresponding investigation tests.

- [ ] Add `provider_tool_call_id` to the persisted model and an internal `allocate_tool_call_id(run_id, provider_id)` helper.
- [ ] Rewrite provider blocks to use the internal ID before transcript append-before-act; use it for idempotency, approvals, evidence operation IDs and recovery.
- [ ] Keep duplicate provider IDs rejected only within one provider turn.
- [ ] Add a regression test that two runs both receiving `tq1` execute successfully with different internal IDs.
- [ ] Run `uv run pytest tests/investigation/test_orchestrator.py tests/investigation/test_store.py tests/investigation/test_transcript.py tests/investigation/test_recovery.py -q`.
- [ ] Commit: `fix(agent): namespace provider tool calls by run`.

### Task 2: Make compaction preserve reacquisition recipes

**Files:** `investigation/types.py`, `investigation/context.py`, `investigation/compactor.py`, `investigation/openai_compactor.py`, context/compactor tests.

- [ ] Add a frozen `ReacquisitionRecipe(purpose, tool_name, arguments, stale_summary)` model.
- [ ] Classify successful read-only logs, config, topology and health observations as reproducible; classify pre-change state, rotated logs, one-time responses and transient resources as immutable.
- [ ] Replace old “reload evidence IDs” stubs for reproducible results with a redacted tool recipe and stale summary.
- [ ] Keep approvals, uncertain/failed tools, Todo, hypotheses, child reports, changesets, backups and recovery state protected.
- [ ] Require semantic compaction output to contain goal, active/rejected hypotheses, reacquisition recipes, immutable observations, pending actions and safety state.
- [ ] Add tests proving post-compaction investigation re-runs `log_query`/`container_read`, while immutable pre-change state uses `evidence_read` only when the source is gone.
- [ ] Run `uv run pytest tests/investigation/test_context.py tests/investigation/test_compactor.py tests/investigation/test_openai_compactor.py -q`.
- [ ] Commit: `feat(context): reacquire reproducible observations after compact`.

### Task 3: Emit the events required by the terminal

**Files:** `events/types.py`, `investigation/events.py`, `investigation/orchestrator.py`, `investigation/context.py`, event/orchestrator tests.

- [ ] Add redacted event types for model rounds, tool proposals, policy decisions, Todo/hypothesis changes and context compaction.
- [ ] Emit events in order: model round, proposal, policy decision, tool start/completion, evidence, compaction and safety state.
- [ ] Include counts, statuses, durations, IDs and bounded previews only; never raw command text where it contains secrets or hidden reasoning.
- [ ] Add ordered-event tests and run `uv run pytest tests/investigation tests/events -q`.
- [ ] Commit: `feat(events): expose auditable agent lifecycle events`.

## Phase 2: One-command colored terminal and recording

### Task 4: Add `incidentlens run`

**Files:** create `cli/run_request.py`; modify `cli/app.py`; add `tests/cli/test_run_command.py`.

- [ ] Parse `run`, `--project`, `--target`, `--service`, `--scope`, `--record` and one symptom string.
- [ ] Resolve Scope only from the registered project/target/service; never accept SSH credentials or Provider secrets from CLI arguments.
- [ ] Create the Investigation and subscribe the TUI before starting the Agent worker so initial events cannot be missed.
- [ ] Test unknown registrations, scope derivation and one-command lifecycle.
- [ ] Run `uv run pytest tests/cli/test_run_command.py tests/cli/test_screens.py -q`.
- [ ] Commit: `feat(cli): launch an investigation from one command`.

### Task 5: Replace snapshot-only output with semantic event cards

**Files:** create `cli/presentation.py` and `cli/widgets/event_card.py`; modify `cli/screens/investigation.py` and `cli/app.py`; add CLI presentation/live-screen tests.

- [ ] Map `◆ MODEL`, `OBSERVE`, `? HYPOTHESIS`, `↳ SUBAGENT`, `⇣ COMPACT`, `⏸ APPROVAL`, `⚙ APPLY`, `↻ RESTART`, `↶ RECOVERY`, `✓ VERIFY`, `■ CONCLUSION` to stable symbols and colors.
- [ ] Use the dark palette from the spec: blue model, cyan remote observation, purple hypothesis/subagent/compact, yellow approval, green success, red failure, gray metadata.
- [ ] Keep a left status pane and append-only right activity stream; update a running tool card by internal ID instead of clearing the log every second.
- [ ] Render redacted arguments, target, duration, result preview, evidence count, diff/impact/verification/rollback and parent-child nesting.
- [ ] Preserve symbols and text when `NO_COLOR=1`.
- [ ] Run `uv run pytest tests/cli/test_presentation.py tests/cli/test_live_screen.py -q`.
- [ ] Commit: `feat(cli): render live semantic agent events`.

### Task 6: Add approval, rollback and synchronized recording

**Files:** modify `cli/screens/investigation.py`; create `cli/recording.py`; modify change command integration; add CLI recording tests.

- [ ] Support `:approve <id>`, `:reject <id>`, `:rollback <changeset-id>` and `:report` through application services, not direct widget access to ChangeManager internals.
- [ ] Start recording before Investigation creation and fan out every input/event to `.cast`, `.trace.jsonl` and ANSI-free `.txt` at event time; flush each record.
- [ ] Redact Provider keys, private paths, IP addresses and raw sensitive output; write `session.interrupted` on clean interruption.
- [ ] Test parseable recording headers, sequence ordering, redaction, `NO_COLOR`, approval continuation and rollback command behavior.
- [ ] Run `uv run pytest tests/cli tests/approvals tests/changes -q`.
- [ ] Commit: `feat(cli): record live investigation terminal sessions`.

## Phase 3: Deterministic dual-regression Docker target

### Task 7: Add stable/canary routing and correlated logs

**Files:** `infra/acceptance/docker-compose.yml`, gateway/order service code, Docker acceptance tests.

- [ ] Add stable and canary order replicas selected deterministically by an opaque route header and preserve `X-Request-ID` through gateway/order logs.
- [ ] Keep both replica health endpoints green and avoid logging the scenario answer.
- [ ] Add routing tests proving stable and canary selection is deterministic.
- [ ] Run `uv run pytest tests/acceptance/test_docker_scenarios.py -k routing -v`.
- [ ] Commit: `feat(acceptance): add deterministic stable canary routing`.

### Task 8: Add two independent non-leaking regressions

**Files:** create `infra/acceptance/config/order-canary.env`, `infra/acceptance/config/payment-policy.env`, `infra/acceptance/scenarios/dual-deployment-regression.yaml`; modify payment service; extend Docker tests.

- [ ] Fault A: canary database port drift causes low and high orders routed to canary to fail while stable succeeds.
- [ ] Fault B: high-value payment requests hit an incorrect policy threshold and return 429/503 while ordinary amounts succeed.
- [ ] Keep health checks green; log request ID, replica, policy version and bounded decisions without printing the expected threshold/root-cause labels.
- [ ] Add a deterministic pre-repair matrix test: stable/normal 201, stable/high failure, canary/normal failure, canary/high failure.
- [ ] Run `uv run pytest tests/acceptance/test_docker_scenarios.py -k 'dual_regression or health' -v`.
- [ ] Commit: `feat(acceptance): add dual deployment regression`.

### Task 9: Add repairable overrides and verification matrix

**Files:** create `infra/acceptance/scripts/request_matrix.py` and `infra/acceptance/compose.cloud.yaml`; modify acceptance README; add matrix tests.

- [ ] Emit JSONL cells with route, amount, request ID, status and served-by; return nonzero unless the expected matrix passes.
- [ ] Put repairable files under the registered protected host scope so File Edit requires backup/approval; bind cloud ports to loopback.
- [ ] Test three consecutive pre-repair and repaired runs for deterministic results.
- [ ] Run `docker compose -f infra/acceptance/docker-compose.yml up -d --build && uv run pytest tests/acceptance -k 'request_matrix or dual_regression' -v`.
- [ ] Commit: `test(acceptance): add reversible four-path verification matrix`.

## Phase 4: Real cloud execution and evidence

### Task 10: Provision only the target services

**Files:** create `scripts/cloud_acceptance_target.sh`; add shell-contract tests.

- [ ] Implement `provision|status|verify-precondition|stop --host <ssh-alias>` with explicit `/opt/incidentlens-target` paths.
- [ ] Copy only `infra/acceptance`, refuse broad/destructive targets, verify loopback port binding, and prove remote `/opt/incidentlens` is absent.
- [ ] Run `shellcheck scripts/cloud_acceptance_target.sh && uv run pytest tests/acceptance/test_cloud_script.py -q`.
- [ ] Commit: `test(cloud): provision target-only acceptance services`.

### Task 11: Trace-based evaluator

**Files:** create `tests/eval/cloud_closed_loop.py`, `tests/eval/test_cloud_closed_loop.py`; modify `tests/eval/types.py`.

- [ ] Evaluate trace/report/matrix without SQLite and correlate by internal run/tool/change/approval IDs.
- [ ] Require two root causes, remote observations after compaction, one SubAgent, zero unapproved mutations, approval-before-mutation, rollback reproduction, reapply and final four-cell success.
- [ ] Give each forbidden condition a named failure; never turn partial output into PASS.
- [ ] Run `uv run pytest tests/eval -q`.
- [ ] Commit: `test(eval): enforce cloud closed-loop invariants`.

### Task 12: Execute, record and publish one real run

**Files:** add redacted `docs/cloud-acceptance/hard-incident/` manifest/final matrix; add `docs/assets/hard-incident.cast`, `.txt`, `.trace.jsonl`, Markdown and HTML report.

- [ ] Provision with `scripts/cloud_acceptance_target.sh provision --host incidentlens-tencent` and verify the four pre-repair matrix cells.
- [ ] Run the exact command from the spec with `INCIDENTLENS_DATA_DIR="$PWD/artifacts/hard-cloud-runtime"`, `--record "$PWD/artifacts/hard-incident.cast"`, project `tencent-cloud-acceptance`, target `tencent-cvm`, service `api-gateway`, host scope, and the approved Chinese symptom; perform all approvals in the same TUI.
- [ ] Confirm the trace shows compaction followed by a fresh remote Observation, both root causes, two approved changes, verification, rollback reproduction, reapply and final matrix success.
- [ ] Redact and hash artifacts; record investigation/run IDs, model, UTC timestamps, target label and evaluator version in the manifest.
- [ ] Run `uv run ruff check . && uv run pytest -q` and the cloud evaluator from the published trace/report/matrix.
- [ ] Update README and runtime verification docs with the honest controlled-scenario scope and limitations.
- [ ] Commit: `docs: publish real hard cloud incident run`.
