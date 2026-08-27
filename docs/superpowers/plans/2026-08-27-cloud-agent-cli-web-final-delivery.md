# Cloud Agent CLI/Web 最终交付重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 CLI/Web 当前未接线和错误恢复实现，完成真实云端调查验收，并以可复核的截图、录制、trace 和测试输出更新项目交付文档。

**Architecture:** FastAPI Control Plane 继续是唯一执行、安全、审批和审计边界。Web 通过只读 HTTP、SSE 和日志 WebSocket 观察权威快照；CLI 通过 HTTP/WS 控制 Agent Session 和精确审批。两端共享 `@incidentlens/protocol`，所有 cursor 都保持服务端不透明值，客户端断线后先恢复权威快照再继续流式观察。

**Tech Stack:** Python 3.12/FastAPI/pytest/Ruff；Node 24/npm workspaces；TypeScript strict；React 19/Vite/TanStack/Playwright；React Ink 7/Ink Testing Library/node-pty；SSH 云端验收。

## Global Constraints

- Web 只能发出 GET、HEAD、OPTIONS；不得出现 Agent 输入、审批、执行、重启、回滚、编辑、Shell 或 Target 配置入口。
- CLI 不直接连接 SSH、不调用模型或远程工具；危险操作只能通过服务端精确审批 API。
- 所有 HTTP DTO 和 stream schema 只能来自 `@incidentlens/protocol`，不得重新手写产品 DTO。
- cursor 是不透明字符串；不得解析、排序、合成或用时间戳、hash、数组索引替换。
- 未验证前不得在 README 或交付索引中声称通过；真实云端证据必须来自本次执行。
- 不向云端目标上传本地源码；任何远程 mutation 都必须在 CLI 显示精确 diff 后由操作者批准。
- 不提交用户的 `.env`、私钥、token、known_hosts 或包含秘密的录制内容。
- 不创建 git commit，除非用户明确要求；每个任务仍必须保持可独立验证。

---

### Task 1: 建立当前基线并锁定失败用例

**Files:**
- Modify: `docs/superpowers/plans/2026-08-24-cloud-agent-cli-web-delivery-index.md`
- Test: `apps/web/e2e/service-logs.spec.ts`
- Test: `apps/web/e2e/stream-recovery.spec.ts`
- Test: `apps/cli/src/app/App.test.tsx`
- Test: `apps/cli/src/stream/session-synchronizer.test.ts`

**Interfaces:**
- Produces a reproducible baseline report from existing commands and explicit failing tests for the disconnected Web log viewer and CLI approval path.

- [ ] **Step 1: Run the existing contract, Python, TypeScript and package checks**

```bash
uv run python scripts/check_product_contracts.py
uv run pytest -q
uv run ruff check .
npm run protocol:check
npm run web:typecheck
npm run web:lint
npm run web:test
npm run web:build
npm run verify:cli
```

Record exit codes and the exact command output outside the repository or in the final evidence directory; do not edit README with results yet.

- [ ] **Step 2: Add a failing Web service-path assertion**

Render/direct-load the service route and assert the page contains the real log viewer controls and a connection attempt to `/ws/v1/logs`, not `LogViewerPlaceholder`.

```tsx
expect(screen.getByRole('region', {name: /日志/i})).toBeVisible();
expect(screen.queryByText(/日志观察组件将在这里显示/i)).toBeNull();
```

- [ ] **Step 3: Add a failing CLI approval-path assertion**

Drive a pending approval event through the App state and assert the rendered approval card exposes approve, reject and full-diff actions, while no decision is marked successful before the server response.

```tsx
expect(lastFrame()).toMatch(/需要审批/);
expect(lastFrame()).toMatch(/\[A\].*批准.*\[R\].*拒绝.*\[D\].*差异/);
```

- [ ] **Step 4: Run only the new tests and confirm they fail for the known reasons**

```bash
npm run web:test -- service.test.tsx
npm test --workspace @incidentlens/cli -- src/app/App.test.tsx
```

Expected failures must identify the placeholder service route and missing App approval registration, not unrelated environment errors.

---

### Task 2: 修复协议 cursor、日志流 live 顺序和 Workspace SSE 认证

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/streams/logs.py`
- Modify: `packages/protocol/src/workspace-events.ts`
- Modify: `apps/web/src/logs/useLiveLogs.ts`
- Test: `tests/streams/test_logs_stream.py`
- Test: `packages/protocol/test/workspace-events.test.ts`
- Test: `apps/web/src/logs/useLiveLogs.test.ts`

**Interfaces:**
- `log.record` processing consumes the stream envelope cursor, not a field inside the redacted log record.
- A log subscription emits backlog records with monotonically delivered product cursor state, then emits the live-ready event.
- Workspace SSE treats HTTP 401/403 as terminal authentication failures and does not retry them.

- [ ] **Step 1: Add backend stream ordering and cursor tests**

Create a subscription fixture with backlog and live records. Assert the first live-ready event occurs after all backlog frames, the envelope cursor is preserved, and initial sequence/cursor is not hard-coded to zero.

- [ ] **Step 2: Add client cursor and gap tests**

Feed frames where `record.cursor` differs from envelope `cursor`; assert the reconnect cursor equals the envelope value. Feed one gap event and assert exactly one authoritative history backfill and one reconnect, with no duplicate backfill.

- [ ] **Step 3: Add terminal SSE auth tests**

Mock EventSource status 401 and 403. Assert the bridge enters an authentication-error state, closes the source, and schedules no reconnect; network errors and 5xx remain retryable.

- [ ] **Step 4: Implement the smallest protocol fixes**

In `logs.py`, initialize and update the product cursor from stream envelopes and emit the live-ready marker only after backlog delivery. In `useLiveLogs.ts`, store `parsed.cursor` and let gap recovery replace the snapshot before reconnecting. In `workspace-events.ts`, inspect the HTTP status before clearing the source reference.

- [ ] **Step 5: Run focused Python and TypeScript tests**

```bash
uv run pytest tests/streams/test_logs_stream.py -q
npm run web:test -- useLiveLogs.test.ts workspace-events.test.ts
```

---

### Task 3: 接通 Web ServicePage 的真实日志工作台

**Files:**
- Modify: `apps/web/src/app/ServicePage.tsx`
- Modify: `apps/web/src/logs/LogViewer.tsx`
- Modify: `apps/web/src/logs/LogToolbar.tsx`
- Modify: `apps/web/src/logs/useLogHistory.ts`
- Modify: `apps/web/src/logs/useLogAnchor.ts`
- Modify: `apps/web/src/logs/VirtualLogViewport.tsx`
- Modify: `apps/web/src/logs/log-viewer.css`
- Delete: `apps/web/src/services/LogViewerPlaceholder.tsx`
- Test: `apps/web/tests/service.test.tsx`
- Test: `apps/web/tests/log-viewer.test.tsx`
- Test: `apps/web/tests/log-recovery.test.tsx`

**Interfaces:**
```ts
export interface LogViewerProps {
  readonly serviceId: string;
  readonly targetId: string;
  readonly initialSearch: LogRouteSearch;
}
```

`LogViewer` must combine `useLogHistory`, `useLiveLogs`, `VirtualLogViewport`, `LogToolbar`, `StreamStatus`, `EvidenceMarker` and `useLogAnchor`; it must receive route search as the authoritative source and write filter changes back through the router.

- [ ] **Step 1: Expand service tests for real UI states**

Assert service direct-load shows health facts plus a named logs region, historical loading/error/empty states, redacted rows, structured JSON/stack expansion, evidence anchor navigation, bounded viewport, reconnecting/resynchronizing state, and absence of mutation controls.

- [ ] **Step 2: Expand URL-state tests**

Start `/services/payment?severity=error&query=timeout&mode=live&follow=1&anchor=abc`. Assert toolbar values match the URL and changing a filter updates `location.search` without local-only state becoming authoritative.

- [ ] **Step 3: Replace the placeholder with the real viewer**

Pass `serviceId`, resolved `targetId`, and route search into `LogViewer`. Remove placeholder imports and ensure no service route can render placeholder copy.

- [ ] **Step 4: Wire history and live data together**

Use the read-only client for historical pages. Subscribe through `/ws/v1/logs` with service/filter scope, preserve opaque product cursor, cap retained live records at 5,000, and render explicit states for connecting, replaying, live, gap recovery, backpressure and terminal auth failure.

- [ ] **Step 5: Apply CLI/Web visual polish without changing safety boundaries**

Use the existing token system to create a dense but calm log workspace: clear header hierarchy, severity text plus icon/color, readable monospace rows, focus-visible controls, dark/light contrast, and responsive narrow layout. Do not add charts or a dashboard wall.

- [ ] **Step 6: Run focused Web tests**

```bash
npm run web:test -- service.test.tsx log-viewer.test.tsx log-recovery.test.tsx
npm run web:typecheck
npm run web:lint
npm run web:build
```

---

### Task 4: 修复 Web E2E 对真实路由的覆盖

**Files:**
- Modify: `apps/web/e2e/fixtures.ts`
- Modify: `apps/web/e2e/service-logs.spec.ts`
- Modify: `apps/web/e2e/stream-recovery.spec.ts`
- Modify: `apps/web/e2e/read-only.spec.ts`

**Interfaces:**
- Fixtures mock the actual `/api/v1` historical GET and `/ws/v1/logs` WebSocket protocol, including envelope cursor, backlog, live-ready, gap and backpressure frames.

- [ ] **Step 1: Replace the nonexistent HTTP stream interception**

Remove `**/api/v1/services/*/logs/stream`; retain only the historical GET and actual WebSocket route. Assert the browser opens `/ws/v1/logs` with the expected subscription frame.

- [ ] **Step 2: Add service golden-path coverage**

Navigate directly to a service URL, verify historical redacted logs, switch filters, observe live records, expand structured data, and follow an evidence marker. Assert no POST/PUT/PATCH/DELETE request occurs.

- [ ] **Step 3: Add disconnect/recovery coverage**

Close the log socket after a known cursor, deliver a gap on reconnect, verify visible resynchronizing state, one authoritative backfill, ordered rows without duplicates, and eventual live state.

- [ ] **Step 4: Run browser E2E**

```bash
npm run web:e2e
```

The run must exercise the actual service route and actual WebSocket path; a green run with no `/ws/v1/logs` request is a failure.

---

### Task 5: 接通 CLI 审批 UI、命令和恢复边界

**Files:**
- Modify: `apps/cli/src/app/App.tsx`
- Modify: `apps/cli/src/app/bootstrap.ts`
- Modify: `apps/cli/src/features/approvals/approval-commands.ts`
- Modify: `apps/cli/src/ui/ApprovalCard.tsx`
- Modify: `apps/cli/src/ui/Conversation.tsx`
- Modify: `apps/cli/src/ui/StatusLine.tsx`
- Modify: `apps/cli/src/state/cli-state.ts`
- Modify: `apps/cli/src/state/reducer.ts`
- Modify: `apps/cli/src/stream/session-synchronizer.ts`
- Test: `apps/cli/src/app/App.test.tsx`
- Test: `apps/cli/src/features/approvals/approval-controller.test.ts`
- Test: `apps/cli/src/app/bootstrap.test.ts`
- Test: `apps/cli/src/stream/session-synchronizer.test.ts`
- Test: `apps/cli/test/pty/approval-flow.test.ts`

**Interfaces:**
```ts
export interface ApprovalCommands {
  approve(reason: string): Promise<ApprovalDetailView>;
  reject(reason: string): Promise<ApprovalDetailView>;
  diff(): Promise<ApprovalDiffView>;
}
```

`App` must render server-authoritative pending approvals and register approval commands alongside target/session commands. Bootstrap must fail closed when principal or compatibility negotiation fails. Session recovery must fetch all pages until the server indicates no next cursor, then subscribe from the latest opaque sequence/cursor.

- [ ] **Step 1: Add failing auth and pagination tests**

Assert a rejected `principal()` prevents ready state and renders a safe authentication error. Mock more than 500 messages/events and assert every page is loaded exactly once with the returned next cursor.

- [ ] **Step 2: Add failing approval command tests**

Assert `/approvals`, `/approve`, `/reject`, `/diff` are discoverable; decision calls include idempotency key and reason; the card remains pending until the server returns the updated approval; stale/expired decisions render a server error.

- [ ] **Step 3: Implement fail-closed bootstrap and complete recovery**

Remove the swallowed principal error. Paginate messages/events using the generated page cursor, preserve order, deduplicate by server IDs, and persist the newest session sequence only after successful processing.

- [ ] **Step 4: Register and render approvals**

Merge `createApprovalCommands` into the command registry. Render a compact OpenCode/Claude-Code-like terminal hierarchy: branded header, connection/session status line, grouped conversation, concise tool summaries, visually distinct approval card, keyboard hints and error banners. Never render raw tool arguments, credentials, provider payloads or unredacted output.

- [ ] **Step 5: Run focused CLI tests and PTY flow**

```bash
npm test --workspace @incidentlens/cli -- src/app/App.test.tsx src/app/bootstrap.test.ts src/features/approvals/approval-controller.test.ts src/stream/session-synchronizer.test.ts
npm run test:pty --workspace @incidentlens/cli -- approval-flow.test.ts
npm run typecheck --workspace @incidentlens/cli
npm run lint
npm run build --workspace @incidentlens/cli
```

---

### Task 6: 完成本地质量门禁和可发布产物检查

**Files:**
- Modify: `docs/superpowers/plans/2026-08-24-cloud-agent-cli-web-delivery-index.md`
- Create outside repository: local verification output directory containing command logs, screenshots and hashes

**Interfaces:**
- Produces a complete local verification matrix; no cloud claim is made unless every required local gate passes.

- [x] **Step 1: Run all contract and backend gates**

```bash
uv run python scripts/check_product_contracts.py
uv run pytest tests/contracts tests/acceptance/test_product_api_foundation.py -q
uv run pytest -q
uv run ruff check apps/control-plane/src tests scripts
```

- [ ] **Step 2: Run all TypeScript gates**

```bash
npm ci
npm run protocol:check
npm run verify:cli
npm run web:typecheck
npm run web:lint
npm run web:test
npm run web:build
npm run web:e2e
npm pack --workspace @incidentlens/cli --dry-run
```

- [x] **Step 3: Verify FastAPI static embedding and wheel contents**

```bash
uv build
uv run pytest tests/web/test_spa_assets.py tests/reports/ tests/acceptance/test_e2e_investigation.py -v
```

Inspect the wheel to confirm hashed SPA assets are present, API/WS/event routes are not swallowed by SPA fallback, and no absolute backend URL is bundled.

- [x] **Step 4: Update the delivery index only with observed results**

Mark individual phases/tasks complete only after their command output and relevant browser/PTY evidence exist. Leave live-model or cloud checks unchecked if skipped.

---

### Task 7: 执行真实云端调查并保存证据

**Files:**
- Use configured local `.env` without committing it
- Create outside repository or in an explicitly non-secret evidence directory: CLI terminal recording, Web screenshots, trace JSONL, plain text transcript, test output and SHA-256 manifest
- Modify: `docs/cloud-acceptance/hard-incident/README.md` only after the run succeeds

**Interfaces:**
- Target: `43.138.132.41`, accessed through the configured SSH identity; server-side changes are made only by the IncidentLens Agent after exact CLI approval.
- Evidence must identify run ID, target, schema/API versions, timestamps, command versions, artifact hashes and pass/fail outcomes without exposing credentials or raw secrets.

- [ ] **Step 1: Verify target access and host identity without mutating the host**

Use the configured SSH command to confirm connectivity and strict host verification. Record only safe identity/capability output; do not upload source or run mutation commands manually.

- [ ] **Step 2: Start the Control Plane and CLI/Web clients with the configured environment**

Launch the API with the required single-worker/persistent data settings, start `incidentlens`, and open the Web workspace independently. Capture clean CLI and Web screenshots after the UI is populated.

- [ ] **Step 3: Add/test the target from CLI**

Use `/target add` and `/target test` with host verification. Confirm the target and discovered services are visible in CLI and the independent Web overview.

- [ ] **Step 4: Run a natural-language investigation without local source**

Investigate the configured cloud incident through the CLI. Confirm Web independently shows redacted historical/live logs, service state, evidence, issue/root cause and investigation progress.

- [ ] **Step 5: Exercise exact approval, validation, rollback/reapply if offered by the scenario**

Inspect `/diff`, approve only the exact displayed change, confirm backup and post-change verification evidence, then exercise native rollback/reapply where the scenario requires it. Reject or stop if the displayed intent differs from the requested scenario.

- [ ] **Step 6: Interrupt and recover both streams independently**

Disconnect CLI WS and Web log WS separately. Verify durable recovery, no silent gap, ordered evidence, and unchanged server investigation state. Restart the Control Plane during safe observation and verify session/operation recovery. If an uncertain mutation is simulated by the existing harness, prove it is marked UNCERTAIN and not automatically replayed.

- [ ] **Step 7: Finalize artifacts and hashes**

Generate a manifest containing SHA-256 for every artifact and scrub secrets before adding documentation references. Keep raw credentials, private keys and `.env` outside the repository.

---

### Task 8: 以真实结果更新 README 和最终交付记录

**Files:**
- Modify: `README.md`
- Modify: `docs/cloud-acceptance/hard-incident/README.md`
- Modify: `docs/superpowers/plans/2026-08-24-cloud-agent-cli-web-delivery-index.md`
- Add only sanitized evidence assets: `docs/assets/...` and `docs/cloud-acceptance/hard-incident/...`

**Interfaces:**
- README becomes a truthful release evidence page: product overview, CLI/Web screenshots, exact verification commands and observed outputs, cloud scenario table, artifact manifest/hash links, limitations and reproducibility instructions.

- [ ] **Step 1: Add visual evidence with captions**

Include one real CLI screenshot showing the branded conversation/approval state and one real Web screenshot showing service health plus redacted logs. Captions must include run context and artifact filename; do not use generated mockups or placeholder screenshots.

- [ ] **Step 2: Add test evidence table**

For each local gate and cloud gate record command, date/run identifier, result, and artifact link. Explicitly state skipped optional live-model tests and any behavior not validated; do not convert skipped into passed.

- [ ] **Step 3: Add cloud investigation record**

Record target identity in redacted form, incident baseline, first fix, rollback/reapply/final verification, approval count, mutation count, evidence count, recovery checks, evaluator result and SHA-256 manifest.

- [ ] **Step 4: Verify documentation links and secret hygiene**

```bash
grep -RInE 'BEGIN (OPENSSH|RSA) PRIVATE KEY|api[_-]?key|bearer |INCIDENTLENS_LLM_API_KEY=|password=' README.md docs/assets docs/cloud-acceptance || true
uv run python - <<'PY'
from pathlib import Path
for path in [Path('README.md'), Path('docs/cloud-acceptance/hard-incident/README.md')]:
    print(path, path.exists(), path.stat().st_size)
PY
```

Open the Markdown locally and verify all image/artifact links resolve.

- [ ] **Step 5: Run the final verification matrix again**

Repeat Task 6 after documentation changes, then report only the commands and artifacts that actually passed.

---

## Completion Gate

The project is ready for delivery only when all of the following are evidenced:

1. ServicePage renders the real bounded log viewer; no placeholder remains in the reachable route graph.
2. CLI visibly handles pending approvals and exact approve/reject/diff actions through server-authoritative state.
3. Cursor, gap, backlog/live ordering, SSE auth and pagination tests pass.
4. Browser E2E uses `/ws/v1/logs`; PTY E2E exercises the approval path.
5. Contract, Python, Ruff, TypeScript, Web build/E2E, CLI packaging and wheel/static checks pass.
6. Real cloud investigation completes against the configured target without uploading local source.
7. CLI and Web are independently interrupted and durably recovered without silent gaps.
8. README and cloud acceptance records contain real screenshots, recordings, traces, outputs and hashes, with skipped or unverified behavior explicitly labeled.
