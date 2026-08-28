# IncidentLens Vite React Log Observability Web Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a directly accessible, strictly read-only Web workspace that clearly presents cloud service state, historical and live redacted logs, issues, evidence, investigation findings, fixes, and verification results.

**Architecture:** Add a Vite React SPA to the existing npm workspace. Consume generated contracts from the single private `@incidentlens/protocol` package; expose only a read-only HTTP facade to Web code. Use TanStack Router for shareable URL state, TanStack Query for authoritative snapshots, one workspace SSE connection for invalidation, cursor HTTP plus a versioned WebSocket for logs, and TanStack Virtual for bounded rendering. Build static assets into the Python package and serve them from FastAPI under the same origin.

**Tech Stack:** React 19, TypeScript strict mode, Vite, TanStack Router/Query/Table/Virtual, Zod, Vitest, React Testing Library, MSW, Playwright, FastAPI StaticFiles

**Spec:** `docs/superpowers/specs/2026-08-24-cloud-agent-cli-web-observability-design.md`

**Backend Prerequisite:** `docs/superpowers/plans/2026-08-24-backend-product-api-foundation.md` is complete.

**Workspace Prerequisite:** Tasks 1–2 of `docs/superpowers/plans/2026-08-24-react-ink-cloud-agent-cli.md` established npm workspaces and `packages/protocol`. If the Web plan is executed before the CLI plan, execute those two shared-infrastructure tasks first, without creating CLI UI behavior.

## Global Constraints

- Web is independently opened and navigated; it never depends on a CLI-generated link or current terminal Session.
- Web is read-only. It does not offer Agent input, approval decisions, SSH/shell, file modification, restart, rollback, deploy, or Target configuration.
- Web HTTP code may issue only GET, HEAD, and OPTIONS. Log WebSocket outbound frames may only subscribe, update filters, pause, resume, and acknowledge browser delivery.
- HTTP DTOs and stream schemas come only from `@incidentlens/protocol`; do not hand-write duplicate server resource models.
- Use relative same-origin `/api/v1`, `/events/v1`, and `/ws/v1` URLs; do not bake an absolute API URL into the bundle.
- URL search state is authoritative for target/service/instance/severity/time/query/mode/anchor/evidence/issue/context/follow.
- Cursors are opaque strings. Never parse, sort, synthesize, or replace them with timestamps/hashes/indexes.
- Unknown stream events are safely ignored. Gap events always trigger authoritative snapshot recovery and a visible resynchronizing state.
- Keep live browser log storage bounded to 5,000 records by default.
- Display only server-redacted content. Never attempt to reconstruct secrets.
- Do not introduce Redux/Zustand, Axios, Next.js, Tailwind, a mega component library, or ECharts in the MVP. Charts can be a later plan after log correctness.
- FastAPI SPA fallback must never swallow `/api`, `/events`, `/ws`, `/assets`, `/healthz`, `/docs`, `/redoc`, `/openapi.json`, or missing asset paths.
- Production runtime requires no Node.js; Node is build/test only.
- Commit only task-owned files.

---

### Task 1: Create the Vite React Package and Test Harness

**Files:**
- Create: `apps/web/package.json`
- Create: `apps/web/index.html`
- Create: `apps/web/tsconfig.json`
- Create: `apps/web/vite.config.ts`
- Create: `apps/web/vitest.config.ts`
- Create: `apps/web/playwright.config.ts`
- Create: `apps/web/eslint.config.js`
- Create: `apps/web/src/main.tsx`
- Create: `apps/web/src/test/setup.ts`
- Create: `apps/web/tests/app-shell.test.tsx`
- Modify: `package.json`
- Modify: `package-lock.json`
- Modify: `.gitignore`

**Interfaces:**
- Produces workspace `@incidentlens/web` and root scripts `web:dev`, `web:test`, `web:typecheck`, `web:lint`, `web:build`, `web:e2e`, `web:verify`.

- [ ] **Step 1: Write a failing shell boundary test**

```tsx
it('identifies the read-only observability workspace', () => {
  render(<App />);
  expect(screen.getByRole('heading', {name: 'IncidentLens'})).toBeVisible();
  expect(screen.getByRole('navigation')).toHaveTextContent('总览');
  expect(screen.queryByRole('button', {name: /approve|reject|execute|restart|rollback/i})).toBeNull();
});
```

- [ ] **Step 2: Install exact Web dependencies**

Install React DOM, TanStack Router/Query/Table/Virtual, Zod, Lucide React, Vite React plugin, Vitest/jsdom, Testing Library, MSW, Playwright, JSX accessibility lint, and types. Reference `@incidentlens/protocol` as a workspace dependency.

- [ ] **Step 3: Configure Vite, strict tests, and minimum App**

Set jsdom, RTL cleanup, jest-dom, deterministic timezone for tests, Vite aliases, and a minimal App with heading/navigation. Do not create mutation APIs or placeholder buttons.

- [ ] **Step 4: Verify package baseline**

```bash
npm ci
npm run web:test -- app-shell.test.tsx
npm run web:typecheck
npm run web:lint
npm run web:build
```

Assert build output contains hashed assets and no absolute backend URL.

- [ ] **Step 5: Commit Web scaffolding**

```bash
git add apps/web package.json package-lock.json .gitignore
git commit -m "build(web): establish Vite observability workspace"
```

---

### Task 2: Expose a Generated Read-Only Web Client

**Files:**
- Create: `packages/protocol/src/web-readonly-client.ts`
- Modify: `packages/protocol/src/index.ts`
- Create: `apps/web/src/api/client.ts`
- Create: `apps/web/src/api/read-only-guard.ts`
- Test: `packages/protocol/test/web-readonly-client.test.ts`
- Test: `apps/web/tests/read-only.test.tsx`

**Interfaces:**

```ts
export interface WebReadonlyClient {
  getOverview(signal?: AbortSignal): Promise<OverviewView>;
  listTargets(signal?: AbortSignal): Promise<TargetPage>;
  listTargetServices(targetId: string, signal?: AbortSignal): Promise<TargetServicePage>;
  getService(serviceId: string, signal?: AbortSignal): Promise<ServiceDetailView>;
  getServiceLogs(serviceId: string, query: ServiceLogQuery, signal?: AbortSignal): Promise<LogPage>;
  listIssues(query: IssueListQuery, signal?: AbortSignal): Promise<IssuePage>;
  getIssue(issueId: string, signal?: AbortSignal): Promise<IssueView>;
  listInvestigations(query: InvestigationListQuery, signal?: AbortSignal): Promise<InvestigationSummaryPage>;
  getInvestigationSummary(id: string, signal?: AbortSignal): Promise<InvestigationSummaryView>;
  getEvidence(id: string, signal?: AbortSignal): Promise<EvidenceDetailView>;
}

export function createWebReadonlyClient(options?: {
  baseUrl?: string;
  fetch?: typeof globalThis.fetch;
}): WebReadonlyClient;
```

- [ ] **Step 1: Write compile/runtime read-only tests**

Assert only approved GET methods are exported, query parameters/cursors remain unchanged, AbortSignal passes through, API errors are safe, and a guarded fetch rejects POST/PUT/PATCH/DELETE with `ReadOnlyViolationError`.

- [ ] **Step 2: Run tests against missing facade**

Run:

```bash
npm test --workspace @incidentlens/protocol -- web-readonly-client.test.ts
npm run web:test -- read-only.test.tsx
```

- [ ] **Step 3: Implement the facade over generated SDK**

Do not export raw generated clients to the Web package. Normalize GET errors without headers/cookies/tokens. Default base URL is `/api/v1`.

- [ ] **Step 4: Add import restrictions**

ESLint for `apps/web` forbids direct imports from generated SDK internals and `openapi-fetch`. All HTTP passes through guarded facade.

- [ ] **Step 5: Verify read-only API boundary**

```bash
npm test --workspace @incidentlens/protocol -- web-readonly-client.test.ts
npm run web:test -- read-only.test.tsx
npm run web:typecheck
npm run web:lint
```

- [ ] **Step 6: Commit the facade**

```bash
git add packages/protocol/src/web-readonly-client.ts packages/protocol/src/index.ts packages/protocol/test/web-readonly-client.test.ts apps/web/src/api apps/web/tests/read-only.test.tsx apps/web/eslint.config.js
git commit -m "feat(web): enforce read-only product API access"
```

---

### Task 3: Add Router, Query Client, and App Shell

**Files:**
- Create: `apps/web/src/router.tsx`
- Create: `apps/web/src/app/AppShell.tsx`
- Create: `apps/web/src/app/Navigation.tsx`
- Create: `apps/web/src/app/RoutePending.tsx`
- Create: `apps/web/src/app/RouteError.tsx`
- Create: `apps/web/src/api/query-keys.ts`
- Create: `apps/web/src/api/queries.ts`
- Create: `apps/web/src/test/render-app.tsx`
- Create: `apps/web/src/test/fixtures.ts`
- Create: `apps/web/src/test/handlers.ts`
- Create: `apps/web/src/test/server.ts`
- Modify: `apps/web/src/main.tsx`
- Modify: `apps/web/tests/app-shell.test.tsx`

**Interfaces:**

Routes:

```text
/                                  OverviewPage
/services/$serviceId               ServicePage
/issues                            IssuesPage
/issues/$issueId                   IssueDetailPage
/investigations/$investigationId   InvestigationPage
```

Query keys are centralized and stable; log keys use canonical filter serialization without cursor parsing.

- [ ] **Step 1: Write direct-load and history tests**

Use memory history/MSW to test each route, browser back/forward, unknown route, pending/error boundaries, and preservation of search state.

- [ ] **Step 2: Run failing router tests**

Run: `npm run web:test -- app-shell.test.tsx`

- [ ] **Step 3: Implement Router and Query defaults**

GET snapshots use finite stale time. Retry network/5xx only; do not retry 401/403/404 blindly. Route/error boundaries never reveal raw response bodies/internal stacks.

- [ ] **Step 4: Build accessible App Shell**

Add main-content skip link, navigation, workspace connection status location, and content outlet. No mutation controls.

- [ ] **Step 5: Verify shell**

```bash
npm run web:test -- app-shell.test.tsx
npm run web:typecheck
npm run web:lint
```

- [ ] **Step 6: Commit shell infrastructure**

```bash
git add apps/web/src/router.tsx apps/web/src/app apps/web/src/api/query-keys.ts apps/web/src/api/queries.ts apps/web/src/test apps/web/src/main.tsx apps/web/tests/app-shell.test.tsx
git commit -m "feat(web): add observable workspace navigation"
```

---

### Task 4: Implement the Cloud Overview

**Files:**
- Create: `apps/web/src/routes/OverviewPage.tsx`
- Create: `apps/web/src/overview/TargetStatusList.tsx`
- Create: `apps/web/src/overview/ServiceStatusTable.tsx`
- Create: `apps/web/src/overview/ActiveIssues.tsx`
- Create: `apps/web/src/overview/RecentResults.tsx`
- Create: `apps/web/src/shared/StatusBadge.tsx`
- Create: `apps/web/src/shared/Timestamp.tsx`
- Test: `apps/web/tests/overview.test.tsx`

**Interfaces:**
- Consumes generated `OverviewView`.
- Produces navigation links to service, issue, and Investigation reads.

- [ ] **Step 1: Write overview state tests**

Test target/host safety labels, service health/version/instances/restarts, active issue severity/status, resolution/verification summaries, links, and distinct states for no Target, no discovered services, no issues, unknown health, and API unavailable.

- [ ] **Step 2: Run failing overview test**

Run: `npm run web:test -- overview.test.tsx`

- [ ] **Step 3: Implement information-dense overview**

Use a table/list hierarchy rather than a KPI-card wall. Every color status also has text/icon. Never display auth refs, keys, raw paths, or provider state.

- [ ] **Step 4: Verify overview**

```bash
npm run web:test -- overview.test.tsx
npm run web:typecheck
npm run web:lint
```

- [ ] **Step 5: Commit overview**

```bash
git add apps/web/src/routes/OverviewPage.tsx apps/web/src/overview apps/web/src/shared/StatusBadge.tsx apps/web/src/shared/Timestamp.tsx apps/web/tests/overview.test.tsx
git commit -m "feat(web): show cloud service overview"
```

---

### Task 5: Implement Service Detail and Read-Only Relationships

**Files:**
- Create: `apps/web/src/routes/ServicePage.tsx`
- Create: `apps/web/src/services/ServiceHeader.tsx`
- Create: `apps/web/src/services/ServiceFacts.tsx`
- Create: `apps/web/src/services/ServiceIssues.tsx`
- Create: `apps/web/src/services/RelatedInvestigations.tsx`
- Test: `apps/web/tests/service.test.tsx`

**Interfaces:**

```ts
export interface LogViewerProps {
  readonly serviceId: string;
  readonly targetId: string;
  readonly initialSearch: LogRouteSearch;
}
```

- [ ] **Step 1: Write direct service-load and safety tests**

Assert target/health/container/version/restart/last observation, issue/Investigation links, waiting approval text “等待 CLI 中的操作者决策”, and absence of approve/reject/restart/rollback/edit/shell buttons.

- [ ] **Step 2: Run failing service test**

Run: `npm run web:test -- service.test.tsx`

- [ ] **Step 3: Implement service composition**

Render generated safe facts and a stable placeholder boundary for `LogViewer`, not a fake log implementation.

- [ ] **Step 4: Verify service view**

```bash
npm run web:test -- service.test.tsx
npm run web:typecheck
```

- [ ] **Step 5: Commit service detail**

```bash
git add apps/web/src/routes/ServicePage.tsx apps/web/src/services apps/web/tests/service.test.tsx
git commit -m "feat(web): show service state and investigations"
```

---

### Task 6: Model Shareable Log Filters and Historical Pagination

**Files:**
- Create: `apps/web/src/logs/log-search.ts`
- Create: `apps/web/src/logs/useLogHistory.ts`
- Create: `apps/web/src/logs/LogViewer.tsx`
- Create: `apps/web/src/logs/LogToolbar.tsx`
- Create: `apps/web/src/logs/StreamStatus.tsx`
- Test: `apps/web/tests/log-search.test.ts`
- Test: `apps/web/tests/log-history.test.tsx`

**Interfaces:**

```ts
export interface LogRouteSearch {
  readonly target?: string;
  readonly instance?: string;
  readonly levels: readonly LogSeverity[];
  readonly from?: string;
  readonly to?: string;
  readonly q?: string;
  readonly mode: 'history' | 'live';
  readonly anchor?: string;
  readonly evidence?: string;
  readonly issue?: string;
  readonly context: number;
  readonly follow: boolean;
}

export function normalizeLogRouteSearch(input: unknown): LogRouteSearch;
```

Defaults: history, empty levels, context 20 (1–100), follow true.

- [ ] **Step 1: Write URL normalization and cursor tests**

Test invalid/empty filters, ISO time validation, from-after-to explanatory error without request, severity arrays, opaque cursor such as `lc1_docker:abc+/==:42`, browser history, and shareable state.

- [ ] **Step 2: Write history query tests**

Test one initial page, exact `next_cursor`, `has_more`, filter key change, AbortSignal cancellation, no client time sorting, error preserving filters, and live mode not creating/deleting subscriptions via POST.

- [ ] **Step 3: Run failing log tests**

Run: `npm run web:test -- log-search.test.ts log-history.test.tsx`

- [ ] **Step 4: Implement URL-driven toolbar and `useInfiniteQuery`**

Controls navigate Router search. Query pages preserve backend order and snapshot cursor. Text request can be debounced, but URL remains authoritative and immediately shareable.

- [ ] **Step 5: Verify history behavior**

```bash
npm run web:test -- log-search.test.ts log-history.test.tsx
npm run web:typecheck
```

- [ ] **Step 6: Commit historical logs**

```bash
git add apps/web/src/logs/log-search.ts apps/web/src/logs/useLogHistory.ts apps/web/src/logs/LogViewer.tsx apps/web/src/logs/LogToolbar.tsx apps/web/src/logs/StreamStatus.tsx apps/web/tests/log-search.test.ts apps/web/tests/log-history.test.tsx
git commit -m "feat(web): query shareable historical logs"
```

---

### Task 7: Add Bounded Log Buffer and Virtualized Viewport

**Files:**
- Create: `apps/web/src/logs/log-buffer.ts`
- Create: `apps/web/src/logs/VirtualLogViewport.tsx`
- Create: `apps/web/src/logs/LogRow.tsx`
- Test: `apps/web/tests/log-buffer.test.ts`
- Test: `apps/web/tests/log-virtualization.test.tsx`

**Interfaces:**

```ts
export interface LogBufferState {
  readonly records: readonly LogRecordView[];
  readonly cursor: string | null;
  readonly droppedBeforeCount: number;
  readonly paused: boolean;
  readonly unreadCount: number;
}

export function reduceLogBuffer(
  state: LogBufferState,
  action: LogBufferAction,
  maxRecords?: number,
): LogBufferState;
```

Default max is 5,000. React keys use `log_id`.

- [ ] **Step 1: Write reducer and virtualization tests**

Test log-ID dedupe, no client sorting, 5,000 eviction, paused buffering/unread, resume preservation, 10,000 fixture with far fewer DOM rows, dynamic height measurement, prepend scroll anchoring, user scroll disabling follow, and “定位最新” behavior.

- [ ] **Step 2: Run failing tests**

Run: `npm run web:test -- log-buffer.test.ts log-virtualization.test.tsx`

- [ ] **Step 3: Implement buffer and TanStack Virtual**

Use `measureElement` for dynamic rows. Preserve first visible item after prepending history. Do not force scroll while the user reads older logs.

- [ ] **Step 4: Verify performance semantics**

```bash
npm run web:test -- log-buffer.test.ts log-virtualization.test.tsx
npm run web:typecheck
```

- [ ] **Step 5: Commit virtualization**

```bash
git add apps/web/src/logs/log-buffer.ts apps/web/src/logs/VirtualLogViewport.tsx apps/web/src/logs/LogRow.tsx apps/web/tests/log-buffer.test.ts apps/web/tests/log-virtualization.test.tsx
git commit -m "feat(web): virtualize bounded log history"
```

---

### Task 8: Render JSON, Stack Traces, and Safe Highlights

**Files:**
- Create: `apps/web/src/logs/log-presentation.ts`
- Create: `apps/web/src/logs/StructuredJson.tsx`
- Create: `apps/web/src/logs/StackTrace.tsx`
- Create: `apps/web/src/shared/highlight.tsx`
- Test: `apps/web/tests/log-presentation.test.tsx`

**Interfaces:**

```ts
export type LogBodyPresentation =
  | {kind: 'json'; value: unknown; summary: string}
  | {kind: 'stack'; headline: string; lines: readonly string[]}
  | {kind: 'text'; text: string};

export function presentLogBody(record: LogRecordView): LogBodyPresentation;
export function highlightSegments(text: string, query: string): readonly HighlightSegment[];
```

- [ ] **Step 1: Write content-safety tests**

Cover `<script>` as text, regex metacharacters, valid/invalid JSON, nested expand/collapse, stack whitespace/order, redaction marker preservation, copy redacted-only content, and no `dangerouslySetInnerHTML` path.

- [ ] **Step 2: Run failing presentation tests**

Run: `npm run web:test -- log-presentation.test.tsx`

- [ ] **Step 3: Implement safe presentation**

Prefer backend structured fields. Parse redacted message as JSON only when structured fields are absent; failure returns text. Do not infer cross-record stacks without a server stable group ID. Copy only current redacted representation.

- [ ] **Step 4: Verify presentation**

```bash
npm run web:test -- log-presentation.test.tsx
npm run web:typecheck
npm run web:lint
```

- [ ] **Step 5: Commit structured logs**

```bash
git add apps/web/src/logs/log-presentation.ts apps/web/src/logs/StructuredJson.tsx apps/web/src/logs/StackTrace.tsx apps/web/src/shared/highlight.tsx apps/web/tests/log-presentation.test.tsx
git commit -m "feat(web): render structured redacted logs"
```

---

### Task 9: Implement Live Log Recovery

**Files:**
- Create: `packages/protocol/src/log-stream.ts`
- Modify: `packages/protocol/src/index.ts`
- Create: `apps/web/src/logs/useLiveLogs.ts`
- Test: `packages/protocol/test/log-stream.test.ts`
- Test: `apps/web/tests/log-live.test.tsx`

**Interfaces:**

```ts
export interface UseLiveLogsResult {
  readonly records: readonly LogRecordView[];
  readonly status: 'connecting' | 'backfilling' | 'live' | 'paused' | 'reconnecting' | 'gap' | 'error';
  readonly unreadCount: number;
  readonly lastCursor: string | null;
  readonly error: Error | null;
  pause(): void;
  resume(): void;
  retry(): void;
}
```

- [ ] **Step 1: Write protocol parser tests**

Validate generated subscribe/update/pause/resume/ack commands and server record/subscribed/heartbeat/gap/slow-consumer events. Unknown valid server events are ignored; malformed/schema mismatch fails.

- [ ] **Step 2: Write recovery state-machine tests**

Test initial HTTP cursor in subscribe, record append/dedupe, heartbeat no row, pause/follow distinction, resume cursor, disconnect → paginated HTTP backfill → reconnect from last cursor → subscribed/live, unknown event, gap authoritative refresh, bounded exponential retry, unmount cleanup, and filter-change socket replacement.

- [ ] **Step 3: Run failing live tests**

```bash
npm test --workspace @incidentlens/protocol -- log-stream.test.ts
npm run web:test -- log-live.test.tsx
```

- [ ] **Step 4: Implement protocol transport and hook**

Do not claim `live` until subscribed/caught-up event. On disconnect, loop HTTP pages until `has_more=false` before reconnecting. On gap, close socket, invalidate/refetch active snapshot, reset safe cursor, then reconnect. WebSocket close never changes server Investigation.

- [ ] **Step 5: Verify live recovery**

```bash
npm test --workspace @incidentlens/protocol -- log-stream.test.ts
npm run web:test -- log-live.test.tsx
npm run web:typecheck
```

- [ ] **Step 6: Commit live logs**

```bash
git add packages/protocol/src/log-stream.ts packages/protocol/src/index.ts packages/protocol/test/log-stream.test.ts apps/web/src/logs/useLiveLogs.ts apps/web/tests/log-live.test.tsx
git commit -m "feat(web): recover realtime logs from durable cursors"
```

---

### Task 10: Link Evidence and Issues to Log Context

**Files:**
- Create: `apps/web/src/logs/useLogAnchor.ts`
- Create: `apps/web/src/logs/EvidenceMarker.tsx`
- Create: `apps/web/src/issues/LogLocatorLink.tsx`
- Test: `apps/web/tests/log-anchor.test.tsx`

**Interfaces:**
- Consumes generated log locator fields on Evidence/Issue/Conclusion/Verification.
- Produces URL navigation to the correct service, cursor, Evidence, Issue, and context size.

- [ ] **Step 1: Write log locator tests**

Test Issue → Evidence → service URL, anchor not in current page causing context fetch/merge/dedupe/center scroll, browser back, refresh restoration, expired cursor fallback to Evidence summary, and locator service mismatch navigating before query.

- [ ] **Step 2: Run failing anchor tests**

Run: `npm run web:test -- log-anchor.test.tsx`

- [ ] **Step 3: Implement URL locator and virtual-scroll bridge**

Never infer location from Evidence text or timestamps. Use only server cursor/log ID. Wait for virtual measurement before centering and highlight the exact row.

- [ ] **Step 4: Verify Evidence navigation**

```bash
npm run web:test -- log-anchor.test.tsx
npm run web:typecheck
```

- [ ] **Step 5: Commit log linkage**

```bash
git add apps/web/src/logs/useLogAnchor.ts apps/web/src/logs/EvidenceMarker.tsx apps/web/src/issues/LogLocatorLink.tsx apps/web/tests/log-anchor.test.tsx
git commit -m "feat(web): locate investigation evidence in logs"
```

---

### Task 11: Implement Issues, Results, and Investigation Reads

**Files:**
- Create: `apps/web/src/routes/IssuesPage.tsx`
- Create: `apps/web/src/routes/IssueDetailPage.tsx`
- Create: `apps/web/src/routes/InvestigationPage.tsx`
- Create: `apps/web/src/issues/IssueSummary.tsx`
- Create: `apps/web/src/issues/EvidenceList.tsx`
- Create: `apps/web/src/issues/ResolutionPanel.tsx`
- Create: `apps/web/src/issues/VerificationPanel.tsx`
- Create: `apps/web/src/investigations/InvestigationSummary.tsx`
- Create: `apps/web/src/investigations/MilestoneTimeline.tsx`
- Create: `apps/web/src/investigations/HypothesisList.tsx`
- Create: `apps/web/src/investigations/InvestigationEvidence.tsx`
- Test: `apps/web/tests/issues.test.tsx`
- Test: `apps/web/tests/investigations.test.tsx`

**Interfaces:**
- Consumes generated `IssueView`, `InvestigationSummaryView`, and `EvidenceDetailView`.

- [ ] **Step 1: Write Issue/detail tests**

Test filters in URL; symptom/impact/root cause/confidence/Evidence/resolution/verification; confidence zero versus null; unknown root cause; verification values passed/failed/inconclusive/not_run; before/after locators; timestamps.

- [ ] **Step 2: Write Investigation tests**

Test status/milestones/hypotheses/evidence/action summaries/conclusion, server sequence ordering, pending approval read-only text, lazy Evidence load, and absence of actions/raw transcript/tool args/provider payload/hidden reasoning.

- [ ] **Step 3: Run failing tests**

Run: `npm run web:test -- issues.test.tsx investigations.test.tsx`

- [ ] **Step 4: Implement read-only result pages**

Render only server projections. Waiting approval says it must be handled in CLI but provides no actionable control or CLI link. Do not infer success when verification is absent/inconclusive.

- [ ] **Step 5: Verify result pages**

```bash
npm run web:test -- issues.test.tsx investigations.test.tsx
npm run web:typecheck
npm run web:lint
```

- [ ] **Step 6: Commit result views**

```bash
git add apps/web/src/routes/IssuesPage.tsx apps/web/src/routes/IssueDetailPage.tsx apps/web/src/routes/InvestigationPage.tsx apps/web/src/issues apps/web/src/investigations apps/web/tests/issues.test.tsx apps/web/tests/investigations.test.tsx
git commit -m "feat(web): show issues findings and verified results"
```

---

### Task 12: Connect One Workspace SSE Invalidation Stream

**Files:**
- Create: `packages/protocol/src/workspace-events.ts`
- Modify: `packages/protocol/src/index.ts`
- Create: `apps/web/src/app/WorkspaceEventBridge.tsx`
- Test: `packages/protocol/test/workspace-events.test.ts`
- Test: `apps/web/tests/workspace-events.test.tsx`

**Interfaces:**

```ts
export interface WorkspaceEventConnection {
  close(): void;
}

export function connectWorkspaceEvents(options: {
  url?: string;
  afterEventId?: string;
  onResourceChanged(event: WorkspaceResourceEvent): void;
  onGap(event: WorkspaceGapEvent): void;
  onStatus(status: 'connecting' | 'live' | 'reconnecting' | 'closed'): void;
}): WorkspaceEventConnection;
```

- [ ] **Step 1: Write single-connection and invalidation tests**

Test one connection for App Shell regardless of route mounts; precise invalidation for service/issue/evidence/Investigation/overview; heartbeat no invalidation; unknown ignore; event ID saved in sessionStorage; gap invalidates all and shows resync; auth failure stops reconnect; network error bounded retry; unmount close.

- [ ] **Step 2: Run failing SSE tests**

```bash
npm test --workspace @incidentlens/protocol -- workspace-events.test.ts
npm run web:test -- workspace-events.test.tsx
```

- [ ] **Step 3: Implement invalidation-only bridge**

SSE payload is a hint, never written as a full business object into Query cache. Final state always comes from GET responses.

- [ ] **Step 4: Verify workspace events**

```bash
npm test --workspace @incidentlens/protocol -- workspace-events.test.ts
npm run web:test -- workspace-events.test.tsx
npm run web:typecheck
```

- [ ] **Step 5: Commit SSE integration**

```bash
git add packages/protocol/src/workspace-events.ts packages/protocol/src/index.ts packages/protocol/test/workspace-events.test.ts apps/web/src/app/WorkspaceEventBridge.tsx apps/web/tests/workspace-events.test.tsx
git commit -m "feat(web): refresh snapshots from workspace events"
```

---

### Task 13: Enforce the Read-Only Boundary End to End

**Files:**
- Modify: `apps/web/src/api/read-only-guard.ts`
- Modify: `apps/web/eslint.config.js`
- Modify: `apps/web/tests/read-only.test.tsx`
- Create: `apps/web/e2e/read-only.spec.ts`

**Interfaces:**
- Runtime guarded fetch rejects any method outside GET/HEAD/OPTIONS.
- Allowed log WS actions are subscribe/update/pause/resume/ack only.

- [ ] **Step 1: Add route-wide UI assertions**

Visit `/`, a service, issues, one issue, and one Investigation. Assert no interactive controls named approve/reject/execute/run/restart/stop/delete/edit/rollback/apply/deploy/open shell. Historical text containing these words is allowed.

- [ ] **Step 2: Add Playwright network assertions**

Capture all HTTP methods and WebSocket outbound frames. Assert no mutation HTTP and only allowed subscription-control frames.

- [ ] **Step 3: Add static import/lint restrictions**

Forbid raw generated clients, direct fetch outside the API boundary, mutation hooks, and server mutation operation IDs from `apps/web`.

- [ ] **Step 4: Verify read-only enforcement**

```bash
npm run web:test -- read-only.test.tsx
npm run web:e2e -- read-only.spec.ts
npm run web:lint
```

- [ ] **Step 5: Commit boundary enforcement**

```bash
git add apps/web/src/api/read-only-guard.ts apps/web/eslint.config.js apps/web/tests/read-only.test.tsx apps/web/e2e/read-only.spec.ts
git commit -m "test(web): enforce read-only observability boundary"
```

---

### Task 14: Complete Visual, Accessibility, and Error States

**Files:**
- Create: `apps/web/src/styles/tokens.css`
- Create: `apps/web/src/styles/global.css`
- Create: `apps/web/src/app/app-shell.css`
- Create: `apps/web/src/logs/log-viewer.css`
- Create: `apps/web/src/shared/EmptyState.tsx`
- Create: `apps/web/src/shared/ErrorNotice.tsx`
- Create: `apps/web/src/shared/LoadingSkeleton.tsx`
- Modify: all page components as needed for semantics
- Test: `apps/web/tests/accessibility.test.tsx`

**Interfaces:**
- Produces a high-density log-first interface with keyboard/accessibility semantics.

- [ ] **Step 1: Write accessibility behavior tests**

Test keyboard filters, expand/collapse with `aria-expanded`, focusable log viewport, visible focus, status text independent of color, reduced motion, 200% zoom smoke, non-spinner empty/error states, and safe error rendering.

- [ ] **Step 2: Run failing accessibility tests**

Run: `npm run web:test -- accessibility.test.tsx`

- [ ] **Step 3: Implement visual system**

Use CSS variables and system sans/mono stacks. Make logs the visual center, not cards. Preserve horizontal log readability on narrow screens. Respect reduced motion. Keep timestamps locally displayed with UTC tooltip.

- [ ] **Step 4: Verify accessibility and visuals**

```bash
npm run web:test -- accessibility.test.tsx
npm run web:typecheck
npm run web:lint
```

- [ ] **Step 5: Commit interface polish**

```bash
git add apps/web/src/styles apps/web/src/app/app-shell.css apps/web/src/logs/log-viewer.css apps/web/src/shared apps/web/src apps/web/tests/accessibility.test.tsx
git commit -m "feat(web): refine accessible log-first interface"
```

---

### Task 15: Embed the Vite Build in FastAPI

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/web_assets.py`
- Create generated directory: `apps/control-plane/src/incidentlens_control_plane/static/web/`
- Modify: `apps/web/vite.config.ts`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/config.py`
- Modify: `pyproject.toml`
- Modify: `.gitignore`
- Test: `tests/web/test_spa_assets.py`

**Interfaces:**

```python
def mount_web_assets(app: FastAPI, *, web_root: Path) -> None:
    """Mount hashed assets and an allow-listed SPA fallback."""
```

- [ ] **Step 1: Write FastAPI asset/fallback tests**

Test root index, service/issue/Investigation deep links, immutable asset cache, no-cache index, missing API remains JSON 404, missing asset remains 404, missing WS route not HTML, and app starts when assets are absent.

- [ ] **Step 2: Run failing backend tests**

Run: `uv run pytest tests/web/test_spa_assets.py tests/test_app.py -q`

- [ ] **Step 3: Configure Vite output and dev proxy**

Build to `apps/control-plane/src/incidentlens_control_plane/static/web`, empty build dir, manifest enabled. Proxy `/api`, `/events`, and `/ws` with WS support in dev. Generated assets are ignored in source control but included in wheel after build.

- [ ] **Step 4: Implement allow-listed SPA fallback**

Mount `/assets/*`; root/deep-link browser GETs return index. Reserved prefixes and filename-like missing paths never fall back. Missing build yields explicit root 404/503 but does not prevent API startup.

- [ ] **Step 5: Verify wheel contents and runtime**

```bash
npm run web:build
uv run pytest tests/web/test_spa_assets.py tests/test_app.py -q
uv build
python -m zipfile -l dist/incidentlens-*.whl
```

Verify wheel contains index and hashed JS/CSS.

- [ ] **Step 6: Commit static integration**

```bash
git add apps/web/vite.config.ts apps/control-plane/src/incidentlens_control_plane/web_assets.py apps/control-plane/src/incidentlens_control_plane/main.py apps/control-plane/src/incidentlens_control_plane/config.py pyproject.toml .gitignore tests/web/test_spa_assets.py
git commit -m "feat(web): serve Vite workspace from FastAPI"
```

---

### Task 16: Add Browser End-to-End Recovery Scenarios

**Files:**
- Modify: `apps/web/playwright.config.ts`
- Create: `apps/web/e2e/fixtures.ts`
- Create: `apps/web/e2e/overview.spec.ts`
- Create: `apps/web/e2e/service-logs.spec.ts`
- Create: `apps/web/e2e/stream-recovery.spec.ts`
- Create: `apps/web/e2e/evidence-navigation.spec.ts`

**Interfaces:**
- Runs production Vite build/preview with deterministic routed HTTP and WebSocket fixtures.

- [ ] **Step 1: Build deterministic browser fixtures**

Use `page.route()` and `page.routeWebSocket()` to model generated contracts, cursor pages, reconnect, gap, Evidence locators, and read-only request capture.

- [ ] **Step 2: Implement end-to-end scenarios**

Cover overview → service; URL filter reload/back; historical prepend without jump; live follow/pause/unread/latest; c10 disconnect, HTTP c11–c15, reconnect c15, live c16 exactly once; gap resync before live; Issue → Evidence → centered log → browser back; and read-only network constraints.

- [ ] **Step 3: Run browser tests**

```bash
npx playwright install chromium
npm run web:build
npm run web:e2e
```

- [ ] **Step 4: Commit E2E coverage**

```bash
git add apps/web/playwright.config.ts apps/web/e2e
git commit -m "test(web): verify browser log recovery workflows"
```

---

### Task 17: Run Full Web and Repository Verification

**Files:**
- Modify: `package.json` only if verification scripts need final alignment
- Modify: CI workflows only if separately part of the active branch scope

- [ ] **Step 1: Verify generated contracts remain current**

```bash
uv run python scripts/check_product_contracts.py
npm run protocol:check
```

- [ ] **Step 2: Run all Web gates**

```bash
npm ci
npm run web:typecheck
npm run web:lint
npm run web:test
npm run web:build
npm run web:e2e
```

- [ ] **Step 3: Run backend/static and full repository regressions**

```bash
uv sync
uv run pytest tests/web/test_spa_assets.py tests/test_app.py -q
uv run pytest tests/web -q
uv run ruff check .
uv run pytest -q
uv build
python -m zipfile -l dist/incidentlens-*.whl
```

- [ ] **Step 4: Verify no generated drift**

```bash
npm run protocol:generate
npm run protocol:check
git diff --exit-code -- packages/protocol
```

- [ ] **Step 5: Commit final script alignment if changed**

```bash
git add package.json package-lock.json
git commit -m "build(web): finalize observability verification"
```

Skip this commit if no files changed.

---

## Web MVP Acceptance

The Web phase is complete only when:

- Users can independently open `/`, service, issue, and Investigation deep links without CLI involvement.
- Overview distinguishes unknown, healthy, degraded, and unreachable state without leaking secrets.
- Historical filters are shareable URL state and use opaque cursor pagination.
- 10,000 fixture logs do not create 10,000 DOM rows.
- JSON and stack traces expand safely; copy contains redacted content only.
- Live logs support follow/pause/unread/latest and bounded memory.
- Disconnect performs HTTP backfill before live reconnect; duplicate records do not appear.
- Gap is visible and forces authoritative resynchronization.
- Evidence, Issue, conclusion, and verification navigate to exact server-provided log cursor/context.
- Issue and Investigation pages clearly show root cause, Evidence, resolution, and verification without inventing confidence or success.
- Waiting approval is visible only as status; no Web page can approve or execute.
- Runtime and E2E guards observe only GET/HEAD/OPTIONS and allowed log subscription-control frames.
- FastAPI serves the static SPA and deep links without swallowing API/stream/asset errors.
- The wheel contains hashed Web assets and production runtime does not require Node.js.
- Full TypeScript, browser, Python, Ruff, contract, and packaging gates pass.
