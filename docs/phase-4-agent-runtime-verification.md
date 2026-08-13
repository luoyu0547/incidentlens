# Phase 4 Agent Runtime Verification

Phase 4 adds the bounded investigation agent runtime: a provider-neutral model
contract, a checkpointed bounded parent/container-child loop, structured
hypotheses/conclusions with evidence-ownership validation, independent
container-scoped child agents with delegated task packages and child reports,
approval pause/resume, source discovery with approval-gated registry proposals,
and startup recovery / orderly shutdown (dangerous in-flight calls are never
replayed).

The runtime is driven by the deterministic `FakeProvider` (scripted steps); no
real model provider is ever contacted. All remote access stays behind the
existing `RemoteToolGateway` / `SessionManager` / `CommandPolicy` /
`ApprovalService` gates, and every agent-visible external fact is an
append-only, redacted `EvidenceRef`.

## Default offline checks

These run on every change and never touch a network. The opt-in live test is
skipped by default.

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/investigation tests/evidence tests/remote_ops tests/logs tests/events tests/web tests/test_app.py -q
UV_CACHE_DIR=.uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane tests
```

Or, the full suite:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests -q
```

The offline suite covers the Phase 4 deterministic acceptance points:

1. The Fake Provider drives a parent typed log/evidence investigation whose
   conclusion cites only evidence the run actually owns.
2. A parent concurrently delegates registered-container children; context,
   budget, session and evidence scope are isolated, and closing a child never
   disturbs the host session.
3. A child returns a grounded report; on crash/over-budget it returns a partial
   report and the parent continues.
4. Any of round/tool/time/output/evidence/no-new-evidence budget exhaustion
   stops the run and never calls the provider or a tool again.
5. Shell/PTTY without approval is never executed; after approval the exact
   single-use intent is consumed once and its output becomes redacted evidence.
6. A dangerous call interrupted by restart is marked UNCERTAIN and never
   replayed; a safe read-only call is repaired and resumable.
7. Unregistered containers / unauthorized paths are never read; an approved
   proposal is re-validated and written back to the registry, then resumed from
   its checkpoint.
8. Runtime restart preserves budgets, hypotheses, evidence refs, children,
   pending approvals/proposals; shutdown order is investigations →
   subscriptions → sessions.
9. REST/WS events carry no raw logs, raw command output, credentials, canonical
   approval intent, backup plaintext or hidden reasoning.
10. All Phase 1-3 tests keep passing.

## Opt-in live checks

The live acceptance test is DISABLED by default. It starts the disposable
`infra/test-ssh` OpenSSH container and drives the real runtime
(`build_runtime`: orchestrator, investigation service, tool executor, evidence,
approvals, recovery) over a real SSH/SFTP/shell transport with the scripted
`FakeProvider`. It verifies:

1. The parent reads a real host log via `log_query`, folds the redacted
   LOG_RECORD evidence into its run, and completes with a grounded conclusion
   citing only evidence the run owns.
2. The parent concurrently delegates two container-scoped children; each child
   runs its own bounded loop against its own scope/session, and its
   evidence-grounded report (COMPLETE when docker is available on the target,
   PARTIAL when the child's container tooling fails) is folded into the parent
   as CHILD_REPORT evidence without disturbing the host session.
3. Approval pause/resume: an approval-required shell command parks the run
   WAITING_APPROVAL; approving re-executes the exact single-use intent once
   (the remote file is actually created) and resumes the run.
4. Restart checkpoint: a fresh runtime over the same `data_dir` restores the
   parked run; the approval decision resumes it from its latest checkpoint and
   round 2 is not replayed (checkpoints 1-4, rounds 1-2).
5. Uncertain no-replay: an in-flight dangerous shell call left by a simulated
   crash is marked UNCERTAIN by startup recovery, the run parks
   PAUSED_UNCERTAIN_STATE, and resuming never re-executes it.

Set `INCIDENTLENS_RUN_LIVE_AGENT_TESTS=1` to opt in:

```bash
INCIDENTLENS_RUN_LIVE_AGENT_TESTS=1 UV_CACHE_DIR=.uv-cache uv run pytest tests/integration/test_live_agent_runtime.py -q
```

Live checks require the existing test SSH/Docker environment and never run by
default. Container-child sub-checks additionally require a `docker` CLI inside
the SSH target; the stock `infra/test-ssh` image installs only OpenSSH, so
against that image the children return PARTIAL reports (a real crash path) and
the test still passes.
