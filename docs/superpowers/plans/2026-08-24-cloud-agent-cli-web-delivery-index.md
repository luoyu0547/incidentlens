# IncidentLens Cloud Agent CLI and Web Delivery Plan Index

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Coordinate delivery of the authenticated backend product contract, React Ink cloud Agent CLI, and Vite React log observability Web in the correct dependency order.

**Architecture:** The FastAPI Control Plane remains the only execution and safety boundary. The CLI controls Agent Sessions and approvals over HTTP/WS; the Web independently observes services, logs, issues, and results over read-only HTTP/SSE/WS. A single generated `packages/protocol` contract package prevents duplicated DTOs and protocol drift.

**Tech Stack:** Python/FastAPI/SQLite/AsyncSSH; TypeScript/npm workspaces; React Ink; Vite React; generated OpenAPI and JSON Schema contracts

**Spec:** `docs/superpowers/specs/2026-08-24-cloud-agent-cli-web-observability-design.md`

## Plans

1. `docs/superpowers/plans/2026-08-24-backend-product-api-foundation.md`
2. `docs/superpowers/plans/2026-08-24-react-ink-cloud-agent-cli.md`
3. `docs/superpowers/plans/2026-08-24-vite-react-log-observability-web.md`

## Shared Boundaries

- The backend plan runs first and publishes deterministic contracts into:
  - `packages/protocol/openapi/v1.json`
  - `packages/protocol/schema/cli-stream-v1.schema.json`
  - `packages/protocol/schema/log-stream-v1.schema.json`
  - `packages/protocol/schema/workspace-stream-v1.schema.json`
- CLI Tasks 1–2 establish the npm workspace and generated private `@incidentlens/protocol` package.
- Web reuses the same workspace and package. It does not create a second `api-client` package or a second OpenAPI generation path.
- CLI may consume mutation endpoints through its `ControlPlaneApi`; Web receives only `WebReadonlyClient`.
- CLI owns interactive approvals. Web may only read Approval status through Investigation/Issue projections.
- Both clients treat backend snapshots as authoritative and streams as recoverable delivery/invalidation channels.
- Legacy `/api/*` routes remain until a separate migration after real client acceptance.

## Execution Order

### Phase A: Backend contract

- [ ] Execute all tasks in `2026-08-24-backend-product-api-foundation.md`.
- [ ] Run backend phase acceptance.
- [ ] Confirm deterministic contract exports and no legacy regressions.

Gate:

```bash
uv run python scripts/check_product_contracts.py
uv run pytest tests/contracts tests/acceptance/test_product_api_foundation.py -q
uv run pytest -q
uv run ruff check .
```

### Phase B: Shared TypeScript infrastructure and CLI

- [ ] Execute React Ink CLI Tasks 1–2 first.
- [ ] Confirm `npm run protocol:check` consumes backend outputs without hand-written DTOs.
- [ ] Execute remaining CLI tasks.
- [ ] Run CLI MVP acceptance.

Gate:

```bash
npm ci
npm run verify:cli
npm pack --workspace @incidentlens/cli --dry-run
uv run pytest -q
uv run ruff check .
```

### Phase C: Log observability Web

- [ ] Execute all Web tasks using the existing workspace/protocol package.
- [ ] Verify HTTP runtime and browser E2E read-only guards.
- [ ] Verify FastAPI static embedding and wheel contents.
- [ ] Run Web MVP acceptance.

Gate:

```bash
npm run protocol:check
npm run web:typecheck
npm run web:lint
npm run web:test
npm run web:build
npm run web:e2e
uv run pytest -q
uv run ruff check .
uv build
```

### Phase D: Real integrated cloud acceptance

- [ ] Configure a remote Target through CLI `/target add` and `/target test` with strict host verification.
- [ ] Do not provide local source code.
- [ ] Start a natural-language investigation from `incidentlens`.
- [ ] Independently open Web and select the corresponding service.
- [ ] Confirm historical/live redacted logs, status, and Agent evidence appear in Web.
- [ ] Approve an exact dangerous change only in CLI.
- [ ] Confirm change, validation, and optional rollback/reapply evidence.
- [ ] Confirm Web shows root cause, resolution, and before/after verification.
- [ ] Interrupt CLI WS and Web log WS independently; verify durable recovery with no silent gap.
- [ ] Restart the control plane during a safe observation and verify Session/Operation recovery.
- [ ] Simulate an uncertain mutation and prove no automatic replay.

## Parallelism After Backend Completion

After backend contracts and CLI Tasks 1–2 are complete:

- CLI implementation and Web implementation may proceed in parallel if each uses isolated worktrees and neither edits `packages/protocol` concurrently.
- Protocol additions are serialized through a dedicated contract task/reviewer.
- FastAPI static embedding belongs to the Web plan; backend domain/API changes belong to the backend plan.
- Run full Python and TypeScript verification after integrating either branch.

## Final Product Acceptance

The coordinated delivery is complete only when:

1. Running `incidentlens` provides natural-language remote cloud investigation and discoverable `/` commands.
2. Local source is optional.
3. CLI is the only interactive execution/approval client.
4. Web independently provides high-quality real log and service/result observation.
5. Neither client duplicates backend execution or safety logic.
6. Streams recover from durable cursors with explicit gap/backpressure semantics.
7. Authentication, authorization, actor audit, idempotency, host verification, exact approval, backup, rollback, and UNCERTAIN no-replay remain enforced server-side.
8. One generated protocol package is the only TypeScript contract source.
9. npm CLI packaging, FastAPI wheel/static packaging, browser E2E, PTY E2E, Python tests, Ruff, and contract drift checks all pass.
10. Existing legacy APIs remain functional until a separately approved removal plan.
