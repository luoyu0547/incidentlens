# IncidentLens Delivery Roadmap

**Source specification:** `docs/superpowers/specs/2026-08-10-incidentlens-remote-diagnostics-design.md`

The approved specification spans several independently reviewable subsystems. Implement them as separate plans so each phase leaves working, testable software and stabilizes the interfaces consumed by the next phase.

## Phase 1: Local Runtime and Project Registry

Build the local process lifecycle, SQLite storage, project/target/service path registry, durable runtime events, and local HTTP/WebSocket API. This phase must not contact a remote server.

Exit criterion: a caller can register and query a Docker Compose project, restart the local Runtime without losing it, and observe registry changes over the shared event stream.

Detailed plan: `docs/superpowers/plans/2026-08-10-local-runtime-project-registry.md`

## Phase 2: Persistent SSH Tools and Safe Changes

Add reusable host SSH sessions, PTY/SFTP channels, remote Read/List/Search/Stat/Edit/Write/Shell tools, command policy, approval records, two-location backups, multi-file ChangeSets, atomic replacement, verification, and rollback.

Exit criterion: a Docker-backed SSH test target supports persistent working-directory state and safe multi-location edits without `vi`, `sed`, one-off Python scripts, or manual `scp`; all service interruptions require approval and every recursive-force `rm` is rejected.

Detailed plan: `docs/superpowers/plans/2026-08-10-persistent-ssh-safe-changes.md`

## Phase 3: Hybrid Log Collection and Evidence Store

Add on-demand Docker/file log queries, opt-in streaming collectors, cursors, SQLite FTS5 indexing, severity parsing, sensitive-data redaction, normal-log signals, service correlation, and immutable evidence references.

Exit criterion: a user can search and stream logs from multiple registered services, distinguish errors/warnings from key normal activity, and retrieve bounded evidence fragments without loading full logs into model context.

## Phase 4: Parent and Container Agent Runtime

Add provider-neutral model interfaces, the bounded investigation loop, structured hypotheses and conclusions, context checkpoints, evidence validation, independent container child Agents, delegated task packages, child reports, cancellation, resumption, and source discovery.

Exit criterion: a parent investigation can delegate a container-scoped task with independent context, receive an evidence-grounded report, and stop safely on budget exhaustion, missing evidence, approval, or uncertain remote state.

## Phase 5: CLI, Web UI, Reports, and End-to-End Acceptance

Add the Claude Code-style interactive CLI, local visual Web UI, shared investigation event timeline, log views, approval and diff screens, source-path management, final reports, recovery flows, and the complete Docker Compose acceptance environment.

Exit criterion: all ten MVP acceptance criteria in the approved specification pass in an end-to-end local demonstration.

## Cross-Phase Rules

- Each phase receives its own detailed implementation plan and review before code changes begin.
- Preserve provider-neutral domain contracts; adapters may depend on domain interfaces, never the reverse.
- Use TDD and commit each independently testable task.
- Do not weaken the approved backup, approval, evidence, credential, or forbidden-command boundaries to simplify a phase.
- Kubernetes, team identity, Git automation, CI/CD, and production deployment remain outside the MVP.

## Specification Coverage

| Approved specification area | Delivery phase |
|---|---|
| Local Runtime, shared API, SQLite, project/source paths | Phase 1 |
| Persistent host SSH/PTTY/SFTP sessions and remote file tools | Phase 2 |
| ChangeSets, two-location backups, policy, approval, rollback | Phase 2 |
| On-demand and streaming logs, FTS5, redaction, evidence storage | Phase 3 |
| Bounded investigation Harness and structured checkpoints | Phase 4 |
| Independent container child Agents and dynamic source discovery | Phase 4 |
| Interactive CLI, visual Web UI, final reports | Phase 5 |
| Failure recovery and security scenarios | Included in every owning phase and Phase 5 end-to-end tests |
| All ten MVP acceptance criteria | Phase 5 completion gate |
