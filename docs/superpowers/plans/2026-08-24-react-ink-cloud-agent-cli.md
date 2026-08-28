# IncidentLens React Ink CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an installable `incidentlens` terminal Agent client that supports natural-language cloud investigation, discoverable slash commands, durable session recovery, and exact CLI-only approvals.

**Architecture:** Build a pure-ESM TypeScript/React Ink client in an npm workspace. Generate all server DTOs and runtime schemas from the checked backend contracts in `packages/protocol`; place HTTP, WebSocket, config, credential, and projection logic behind testable boundaries. The CLI never performs SSH/model/tool execution and never becomes a full log dashboard.

**Tech Stack:** Node.js `>=22.19.0`, npm workspaces, TypeScript strict mode, React 19, Ink 7, generated Fetch client + Zod, Vitest, Ink Testing Library, node-pty, tsup

**Spec:** `docs/superpowers/specs/2026-08-24-cloud-agent-cli-web-observability-design.md`

**Backend Prerequisite:** `docs/superpowers/plans/2026-08-24-backend-product-api-foundation.md` is complete and `uv run python scripts/check_product_contracts.py` passes.

## Global Constraints

- Root JavaScript tooling uses npm workspaces and one committed `package-lock.json`; do not add pnpm, Yarn, or Bun lockfiles.
- Use Node 24 LTS for development; published package engine floor is exactly `>=22.19.0`.
- Use pure ESM and TypeScript `strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, and `useUnknownInCatchVariables`.
- HTTP DTOs and stream schemas come only from `packages/protocol`; never hand-write duplicate server resource interfaces in `apps/cli`.
- The CLI sends natural language to Agent Sessions; it never selects or executes SSH, Docker, file, shell, or model tools itself.
- Closing the CLI, Ctrl+C, stdin close, or WebSocket disconnect never cancels server work. Only `/cancel` calls cancellation.
- Do not render unbounded logs, charts, a multi-panel dashboard, Web links, or an “open browser” action.
- Do not display raw tool arguments/output, provider payloads, hidden reasoning, credentials, unredacted logs, or canonical approval intent.
- Approval state is server-authoritative; never optimistically mark a decision successful.
- Token storage must use OS credential storage or an explicit session/environment source; never fall back to plaintext config.
- Bun compile is outside MVP. First ship and verify the npm package.
- Commit steps below must stage only files belonging to that task.

---

### Task 1: Create the npm Workspace and Executable CLI Package

**Files:**
- Create: `package.json`
- Create: `package-lock.json`
- Create: `.nvmrc`
- Create: `tsconfig.base.json`
- Create: `eslint.config.mjs`
- Create: `prettier.config.mjs`
- Create: `apps/cli/package.json`
- Create: `apps/cli/tsconfig.json`
- Create: `apps/cli/tsup.config.ts`
- Create: `apps/cli/vitest.config.ts`
- Create: `apps/cli/src/cli.tsx`
- Create: `apps/cli/test/package-metadata.test.ts`
- Modify: `.gitignore`

**Interfaces:**
- Produces the `@incidentlens/cli` workspace and executable `apps/cli/dist/cli.js`.
- Root workspaces are `apps/*` and `packages/*`; `packages/protocol` is populated in Task 2.

- [ ] **Step 1: Write package metadata tests before creating metadata**

```ts
import {readFile} from 'node:fs/promises';
import {describe, expect, it} from 'vitest';

it('publishes the incidentlens executable for supported Node versions', async () => {
  const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  expect(pkg.name).toBe('@incidentlens/cli');
  expect(pkg.bin).toEqual({incidentlens: './dist/cli.js'});
  expect(pkg.engines).toEqual({node: '>=22.19.0'});
  expect(pkg.type).toBe('module');
});
```

Add a built-file assertion for `#!/usr/bin/env node`.

- [ ] **Step 2: Create workspace metadata and install exact dependencies**

Root scripts:

```json
{
  "build": "npm run build --workspaces --if-present",
  "typecheck": "npm run typecheck --workspaces --if-present",
  "lint": "eslint .",
  "format:check": "prettier --check .",
  "test": "npm run test --workspaces --if-present",
  "protocol:generate": "npm run generate --workspace @incidentlens/protocol",
  "protocol:check": "npm run check --workspace @incidentlens/protocol",
  "test:pty": "npm run test:pty --workspace @incidentlens/cli",
  "verify:cli": "npm run protocol:check && npm run lint && npm run format:check && npm run typecheck && npm test && npm run build && npm run test:pty"
}
```

Install React, Ink, `ws`, Zod, tsup, TypeScript, Vitest, Ink Testing Library, ESLint, typescript-eslint, React Hooks lint, Prettier, and relevant Node/React types using `npm install` so the lockfile records actual resolved versions.

- [ ] **Step 3: Configure strict TypeScript and linting**

Use NodeNext modules, `jsx: react-jsx`, `target: ES2023`, strict flags from Global Constraints, type-aware ESLint, no floating promises, React Hooks rules, and no focused tests. Ignore generated protocol files for lint only, not typecheck/drift checks.

- [ ] **Step 4: Implement the minimum executable**

`cli.tsx` validates Node version, handles `--version`, and renders a temporary `IncidentLens` Ink component. `tsup` produces one ESM entry with shebang and source map.

- [ ] **Step 5: Verify workspace reproducibility**

```bash
npm ci
npm test --workspace @incidentlens/cli -- package-metadata.test.ts
npm run typecheck
npm run lint
npm run build --workspace @incidentlens/cli
node apps/cli/dist/cli.js --version
```

- [ ] **Step 6: Commit workspace scaffolding**

```bash
git add package.json package-lock.json .nvmrc tsconfig.base.json eslint.config.mjs prettier.config.mjs apps/cli .gitignore
git commit -m "build(cli): establish React Ink workspace"
```

---

### Task 2: Generate the Shared Product Protocol

**Files:**
- Create: `packages/protocol/package.json`
- Create: `packages/protocol/tsconfig.json`
- Create: `packages/protocol/openapi-ts.config.ts`
- Use backend output: `packages/protocol/openapi/v1.json`
- Use backend output: `packages/protocol/schema/cli-stream-v1.schema.json`
- Use backend output: `packages/protocol/schema/log-stream-v1.schema.json`
- Create: `packages/protocol/src/generated/`
- Create: `packages/protocol/src/stream.ts`
- Create: `packages/protocol/src/index.ts`
- Create: `packages/protocol/scripts/check-generated.mjs`
- Test: `packages/protocol/test/contract.test.ts`

**Interfaces:**

```ts
export type ParsedStreamEvent =
  | {kind: 'known'; envelope: KnownCliStreamEnvelope}
  | {kind: 'unknown'; envelope: CliStreamEnvelopeBase};

export function parseStreamFrame(raw: string): ParsedStreamEvent;
export function assertCompatible(server: ApiVersionView, client: ClientCompatibility): void;
```

- [ ] **Step 1: Write a failing contract-readiness test**

Assert required stable operation IDs exist for version, principal, targets, target test, sessions/messages, operations, approvals, and event history. Assert stream schemas discriminate by `event_type`, require sequence/schema version, and include gap/heartbeat/slow-consumer.

- [ ] **Step 2: Run the gate against backend contract files**

Run: `npm test --workspace @incidentlens/protocol`

Expected: FAIL until the package/generation exists; if backend contracts are incomplete, stop this plan and finish the backend plan rather than inventing DTOs.

- [ ] **Step 3: Generate types, Fetch SDK, and Zod schemas**

Configure Hey API plugins for TypeScript, SDK, Fetch client, and Zod. Keep `@incidentlens/protocol` private. Export only reviewed generated types/functions and stream parsers.

- [ ] **Step 4: Implement strict stream parsing**

Malformed JSON, missing sequence, and unsupported schema are protocol failures. Known variants validate with generated/runtime schemas. Valid unknown event types retain base IDs/sequence and return `kind: 'unknown'`, allowing the cursor to advance safely.

- [ ] **Step 5: Add deterministic drift checking**

`check-generated.mjs` regenerates into a temporary directory and byte-compares committed generated files. It must not rewrite files during `check`.

- [ ] **Step 6: Verify protocol generation and types**

```bash
npm run protocol:generate
npm run protocol:check
npm test --workspace @incidentlens/protocol
npm run typecheck --workspace @incidentlens/protocol
```

- [ ] **Step 7: Commit generated protocol**

```bash
git add packages/protocol package.json package-lock.json
git commit -m "build(protocol): generate product API contracts"
```

---

### Task 3: Store Profiles and Tokens Safely

**Files:**
- Create: `apps/cli/src/config/types.ts`
- Create: `apps/cli/src/config/config-store.ts`
- Create: `apps/cli/src/config/file-config-store.ts`
- Create: `apps/cli/src/auth/token-store.ts`
- Create: `apps/cli/src/auth/keyring-token-store.ts`
- Create: `apps/cli/src/auth/environment-token-store.ts`
- Test: `apps/cli/src/config/config-store.test.ts`
- Test: `apps/cli/src/auth/token-store.test.ts`

**Interfaces:**

```ts
export interface ProfileConfig {
  readonly profileName: string;
  readonly apiUrl: string;
  readonly lastTargetId?: string;
  readonly lastSessionId?: string;
  readonly lastSequenceBySession: Readonly<Record<string, number>>;
}

export interface ConfigStore {
  load(profileName: string): Promise<ProfileConfig | null>;
  save(profile: ProfileConfig): Promise<void>;
}

export interface TokenStore {
  get(profileName: string): Promise<string | null>;
  set(profileName: string, token: string): Promise<void>;
  delete(profileName: string): Promise<void>;
}
```

- [ ] **Step 1: Write security and atomicity tests**

Test profile round trip, URL normalization, per-session cursor preservation, mode `0600` on Unix, temp-file atomic replacement, token absence from JSON/errors, environment token read-only behavior, and keyring failure without plaintext fallback.

- [ ] **Step 2: Run failing tests**

Run: `npm test --workspace @incidentlens/cli -- src/config/config-store.test.ts src/auth/token-store.test.ts`

- [ ] **Step 3: Implement platform config storage**

Use the OS user config directory, Zod-validate on read, normalize API URL without credentials/query/fragment, write a sibling temp file with restricted mode, fsync/rename, and preserve unknown future profile files by selecting one named profile only.

- [ ] **Step 4: Implement credential boundaries**

Use `@napi-rs/keyring` (or the exact selected OS-keyring dependency) behind `TokenStore`. `INCIDENTLENS_TOKEN` is a read-only alternative for CI/headless runs. If keyring is unavailable, return a typed `CredentialStoreUnavailable` so UI requests a session-only token; never write plaintext.

- [ ] **Step 5: Verify secure storage**

```bash
npm test --workspace @incidentlens/cli -- src/config/config-store.test.ts src/auth/token-store.test.ts
npm run typecheck --workspace @incidentlens/cli
npm run lint
```

- [ ] **Step 6: Commit storage**

```bash
git add apps/cli/src/config apps/cli/src/auth apps/cli/package.json package-lock.json
git commit -m "feat(cli): persist profiles without plaintext credentials"
```

---

### Task 4: Implement the Typed Control Plane HTTP Client

**Files:**
- Create: `apps/cli/src/api/control-plane-api.ts`
- Create: `apps/cli/src/api/generated-control-plane-api.ts`
- Create: `apps/cli/src/api/api-error.ts`
- Create: `apps/cli/src/api/idempotency.ts`
- Create: `apps/cli/test/contract/http-server.ts`
- Test: `apps/cli/src/api/generated-control-plane-api.test.ts`

**Interfaces:**

```ts
export interface MutationOptions {
  readonly idempotencyKey: string;
  readonly signal?: AbortSignal;
}

export interface ControlPlaneApi {
  compatibility(signal?: AbortSignal): Promise<ApiVersionView>;
  principal(signal?: AbortSignal): Promise<PrincipalView>;
  listTargets(signal?: AbortSignal): Promise<TargetPage>;
  createTarget(input: TargetCreate, options: MutationOptions): Promise<TargetView>;
  updateTarget(id: string, input: TargetPatch, options: MutationOptions): Promise<TargetView>;
  removeTarget(id: string, options: MutationOptions): Promise<void>;
  testTarget(id: string, options: MutationOptions): Promise<OperationAccepted>;
  createSession(input: AgentSessionCreate, options: MutationOptions): Promise<AgentSessionView>;
  listSessions(query: AgentSessionListQuery, signal?: AbortSignal): Promise<AgentSessionPage>;
  getSession(id: string, signal?: AbortSignal): Promise<AgentSessionView>;
  listMessages(id: string, query: MessageListQuery, signal?: AbortSignal): Promise<AgentMessagePage>;
  sendMessage(id: string, input: AgentMessageCreate, options: MutationOptions): Promise<AgentMessageAccepted>;
  resumeSession(id: string, options: MutationOptions): Promise<OperationAccepted>;
  cancelSession(id: string, options: MutationOptions): Promise<OperationView>;
  getOperation(id: string, signal?: AbortSignal): Promise<OperationView>;
  listApprovals(query: ApprovalListQuery, signal?: AbortSignal): Promise<ApprovalPage>;
  getApproval(id: string, signal?: AbortSignal): Promise<ApprovalDetailView>;
  decideApproval(id: string, decision: 'approve' | 'reject', input: ApprovalDecisionRequest, options: MutationOptions): Promise<ApprovalDetailView>;
}
```

- [ ] **Step 1: Write HTTP contract tests**

Test bearer injection, no token in error messages, request ID preservation, generated runtime validation, GET retry only for network/408/429/5xx, mutation no automatic retry, explicit retry key reuse, AbortError preservation, and compatibility before business calls.

- [ ] **Step 2: Run failing client tests**

Run: `npm test --workspace @incidentlens/cli -- src/api/generated-control-plane-api.test.ts`

- [ ] **Step 3: Implement generated SDK adapter**

Components import `ControlPlaneApi`, never generated endpoint functions. Normalize error envelope to:

```ts
export class ApiError extends Error {
  readonly code: string;
  readonly requestId?: string;
  readonly status?: number;
  readonly details: unknown;
  readonly retryable: boolean;
}
```

Create idempotency keys with `crypto.randomUUID()` and retain them with pending user actions so explicit retry reuses the key.

- [ ] **Step 4: Verify client behavior**

```bash
npm test --workspace @incidentlens/cli -- src/api/generated-control-plane-api.test.ts
npm run typecheck --workspace @incidentlens/cli
npm run lint
```

- [ ] **Step 5: Commit the HTTP client**

```bash
git add apps/cli/src/api apps/cli/test/contract/http-server.ts
git commit -m "feat(cli): add typed control plane client"
```

---

### Task 5: Build the Slash Command Parser and Palette

**Files:**
- Create: `apps/cli/src/commands/types.ts`
- Create: `apps/cli/src/commands/registry.ts`
- Create: `apps/cli/src/commands/parser.ts`
- Create: `apps/cli/src/commands/execute-command.ts`
- Create: `apps/cli/src/ui/CommandPalette.tsx`
- Test: `apps/cli/src/commands/parser.test.ts`
- Test: `apps/cli/src/commands/registry.test.ts`
- Test: `apps/cli/src/ui/CommandPalette.test.tsx`

**Interfaces:**

```ts
export interface SlashCommand {
  readonly path: readonly string[];
  readonly summary: string;
  readonly group: 'help' | 'target' | 'connection' | 'session' | 'scope' | 'investigation' | 'approval' | 'system';
  readonly usage: string;
  readonly dangerous: boolean;
  available(context: CommandContext): boolean;
  execute(invocation: CommandInvocation, context: CommandContext): Promise<CommandResult>;
}

export type ParsedInput =
  | {kind: 'empty'}
  | {kind: 'message'; text: string}
  | {kind: 'command'; invocation: CommandInvocation}
  | {kind: 'incomplete-command'; query: string};
```

- [ ] **Step 1: Write parser and interaction tests**

Cover `/`, `/tar`, longest match (`/target add`), quotes/escaped spaces, empty args, unknown slash never becoming an Agent message, ordinary Chinese/pasted text becoming a message, capability filtering, arrows, Tab, Enter, Esc, and dangerous confirmation.

- [ ] **Step 2: Run failing tests**

Run:

```bash
npm test --workspace @incidentlens/cli -- src/commands/parser.test.ts src/commands/registry.test.ts src/ui/CommandPalette.test.tsx
```

- [ ] **Step 3: Implement deterministic parsing and registry**

Register only commands with working handlers and advertised backend capabilities. Initial set: `/help`, `/status`, `/target`, `/target add|edit|test|remove`, `/new`, `/sessions`, `/resume`, `/rename`, `/clear`, `/cancel`, `/approvals`, `/approve`, `/reject`, `/diff`, `/reconnect`, `/exit`.

Commands such as `/services`, `/scope`, `/plan`, `/todos`, `/evidence`, `/hypotheses`, `/model`, `/compact`, or full `/doctor` enter the registry only when their backend operation IDs exist; do not add disabled placeholders.

- [ ] **Step 4: Verify command discovery**

```bash
npm test --workspace @incidentlens/cli -- src/commands src/ui/CommandPalette.test.tsx
npm run typecheck --workspace @incidentlens/cli
```

- [ ] **Step 5: Commit command infrastructure**

```bash
git add apps/cli/src/commands apps/cli/src/ui/CommandPalette.tsx apps/cli/src/ui/CommandPalette.test.tsx
git commit -m "feat(cli): add discoverable slash commands"
```

---

### Task 6: Create the Pure CLI Projection Reducer

**Files:**
- Create: `apps/cli/src/state/cli-state.ts`
- Create: `apps/cli/src/state/reducer.ts`
- Create: `apps/cli/src/state/selectors.ts`
- Test: `apps/cli/src/state/reducer.test.ts`

**Interfaces:**

```ts
export interface CliState {
  readonly bootstrap: 'loading' | 'ready' | 'authentication-required' | 'incompatible';
  readonly target?: TargetView;
  readonly session?: AgentSessionView;
  readonly messages: readonly ConversationItem[];
  readonly operations: Readonly<Record<string, OperationView>>;
  readonly approvals: Readonly<Record<string, ApprovalDetailView>>;
  readonly stream: StreamStatus;
  readonly input: InputState;
  readonly overlay: OverlayState;
}
```

`ConversationItem` is a safe UI projection with server IDs and summaries, not a duplicate server DTO.

- [ ] **Step 1: Write event-reduction tests**

Cover text delta merge by message/block ID, finalization, duplicate event and old sequence rejection, same-card tool transitions, same-card Approval transitions, unknown event cursor advance without UI mutation, gap snapshot replacement, and no raw payload retention.

- [ ] **Step 2: Run failing reducer tests**

Run: `npm test --workspace @incidentlens/cli -- src/state/reducer.test.ts`

- [ ] **Step 3: Implement immutable deterministic projection**

The reducer consumes only parsed known envelopes and authoritative HTTP snapshots. Track last committed sequence separately from visible items. Do not store entire raw envelopes after application.

- [ ] **Step 4: Verify reducer**

```bash
npm test --workspace @incidentlens/cli -- src/state/reducer.test.ts
npm run typecheck --workspace @incidentlens/cli
```

- [ ] **Step 5: Commit state projection**

```bash
git add apps/cli/src/state
git commit -m "feat(cli): project durable Agent events"
```

---

### Task 7: Implement the Minimal Ink Conversation Shell

**Files:**
- Create: `apps/cli/src/app/dependencies.ts`
- Create: `apps/cli/src/app/bootstrap.ts`
- Create: `apps/cli/src/app/App.tsx`
- Create: `apps/cli/src/input/input-controller.ts`
- Create: `apps/cli/src/input/use-input-routing.ts`
- Create: `apps/cli/src/ui/Conversation.tsx`
- Create: `apps/cli/src/ui/PromptInput.tsx`
- Create: `apps/cli/src/ui/StatusLine.tsx`
- Modify: `apps/cli/src/cli.tsx`
- Test: `apps/cli/src/app/App.test.tsx`
- Test: `apps/cli/src/ui/Conversation.test.tsx`
- Test: `apps/cli/src/ui/PromptInput.test.tsx`

**Interfaces:**

```ts
export interface AppDependencies {
  readonly api: ControlPlaneApi;
  readonly configStore: ConfigStore;
  readonly tokenStore: TokenStore;
  readonly eventStream: EventStream;
  readonly now: () => Date;
  readonly exit: () => void;
}
```

- [ ] **Step 1: Write shell/input behavior tests**

Assert IncidentLens header, current target/session/status, help hint, Chinese input preservation, empty Enter no-op, ordinary text routes to `sendMessage`, slash does not, Ctrl+C closes overlays/clears input/exits when idle, and no exit path invokes cancel. Assert no Web URL, log table, chart, or dashboard panel.

- [ ] **Step 2: Run failing Ink tests**

Run:

```bash
npm test --workspace @incidentlens/cli -- src/app/App.test.tsx src/ui/Conversation.test.tsx src/ui/PromptInput.test.tsx
```

- [ ] **Step 3: Implement single-column shell and input routing**

Render conversation, thin status line, overlays, and one prompt. `/clear` clears only local visible projection. Approval hotkeys are inactive in this task.

- [ ] **Step 4: Verify rendering and type safety**

```bash
npm test --workspace @incidentlens/cli -- src/app src/ui/Conversation.test.tsx src/ui/PromptInput.test.tsx
npm run typecheck --workspace @incidentlens/cli
npm run build --workspace @incidentlens/cli
```

- [ ] **Step 5: Commit the Ink shell**

```bash
git add apps/cli/src/app apps/cli/src/input apps/cli/src/ui/Conversation.tsx apps/cli/src/ui/PromptInput.tsx apps/cli/src/ui/StatusLine.tsx apps/cli/src/cli.tsx
git commit -m "feat(cli): render interactive Agent conversation"
```

---

### Task 8: Implement Target Commands and Wizard

**Files:**
- Create: `apps/cli/src/features/targets/target-commands.ts`
- Create: `apps/cli/src/features/targets/target-controller.ts`
- Create: `apps/cli/src/ui/TargetWizard.tsx`
- Test: `apps/cli/src/features/targets/target-controller.test.ts`
- Test: `apps/cli/src/ui/TargetWizard.test.tsx`

**Interfaces:**

```ts
export interface TargetController {
  list(signal?: AbortSignal): Promise<readonly TargetView[]>;
  select(target: TargetView): Promise<void>;
  create(input: TargetCreate): Promise<TargetView>;
  update(id: string, input: TargetPatch): Promise<TargetView>;
  test(id: string): Promise<OperationAccepted>;
  remove(id: string): Promise<void>;
}
```

- [ ] **Step 1: Write target flow tests**

Test picker, `/target production`, wizard sequence for name/host/user/port/auth reference/host-key policy, no private-key input, host-key test result, persisted last target, edit version, deletion confirmation, and idempotent retry.

- [ ] **Step 2: Run failing tests**

Run: `npm test --workspace @incidentlens/cli -- src/features/targets src/ui/TargetWizard.test.tsx`

- [ ] **Step 3: Implement target controller and slash handlers**

The wizard sends metadata and an opaque auth reference only. `/target test` tracks returned Operation and clearly reports verified host-key source/fingerprint or safe failure. Delete requires a typed/explicit confirmation overlay.

- [ ] **Step 4: Verify target UX**

```bash
npm test --workspace @incidentlens/cli -- src/features/targets src/ui/TargetWizard.test.tsx
npm run typecheck --workspace @incidentlens/cli
```

- [ ] **Step 5: Commit target UX**

```bash
git add apps/cli/src/features/targets apps/cli/src/ui/TargetWizard.tsx apps/cli/src/ui/TargetWizard.test.tsx
git commit -m "feat(cli): manage remote targets with slash commands"
```

---

### Task 9: Implement Agent Sessions, Messages, and Operation Tracking

**Files:**
- Create: `apps/cli/src/features/sessions/session-controller.ts`
- Create: `apps/cli/src/features/sessions/session-commands.ts`
- Create: `apps/cli/src/features/sessions/operation-tracker.ts`
- Create: `apps/cli/src/ui/SessionPicker.tsx`
- Test: `apps/cli/src/features/sessions/session-controller.test.ts`
- Test: `apps/cli/src/features/sessions/operation-tracker.test.ts`
- Test: `apps/cli/src/ui/SessionPicker.test.tsx`

**Interfaces:**

```ts
export interface SessionController {
  sendNaturalLanguage(text: string): Promise<AgentMessageAccepted>;
  create(title?: string): Promise<AgentSessionView>;
  select(session: AgentSessionView): Promise<void>;
  resume(sessionId: string): Promise<OperationAccepted>;
  cancelCurrent(): Promise<OperationView>;
  rename(title: string): Promise<AgentSessionView>;
}
```

- [ ] **Step 1: Write session lifecycle tests**

Test no-target blocking, create-on-first-message, subsequent messages in same Session, operation tracking, `/new`, `/sessions`, `/resume`, `/rename`, explicit `/cancel`, `/exit` no cancellation, message retry idempotency, and restoration of last target/session.

- [ ] **Step 2: Run failing tests**

Run: `npm test --workspace @incidentlens/cli -- src/features/sessions src/ui/SessionPicker.test.tsx`

- [ ] **Step 3: Implement server-authoritative session orchestration**

Do not create Investigation/Run locally. Use Agent Session endpoints; store returned IDs and pending idempotency key. `/resume` attaches and requests server recovery. `/cancel` is the only path to cancel API.

- [ ] **Step 4: Verify session flows**

```bash
npm test --workspace @incidentlens/cli -- src/features/sessions src/ui/SessionPicker.test.tsx
npm run typecheck --workspace @incidentlens/cli
```

- [ ] **Step 5: Commit session flows**

```bash
git add apps/cli/src/features/sessions apps/cli/src/ui/SessionPicker.tsx apps/cli/src/ui/SessionPicker.test.tsx
git commit -m "feat(cli): control durable Agent sessions"
```

---

### Task 10: Implement Recoverable WebSocket Synchronization

**Files:**
- Create: `apps/cli/src/stream/event-stream.ts`
- Create: `apps/cli/src/stream/ws-event-stream.ts`
- Create: `apps/cli/src/stream/reconnect-policy.ts`
- Create: `apps/cli/src/stream/session-synchronizer.ts`
- Create: `apps/cli/test/contract/ws-server.ts`
- Test: `apps/cli/src/stream/ws-event-stream.test.ts`
- Test: `apps/cli/src/stream/session-synchronizer.test.ts`

**Interfaces:**

```ts
export interface StreamCursor {
  readonly sessionId: string;
  readonly sequence: number;
}

export interface EventStream {
  connect(
    cursor: StreamCursor,
    handlers: {
      onEvent(event: KnownCliStreamEnvelope): Promise<void> | void;
      onGap(gap: StreamGap): Promise<void>;
      onStatus(status: StreamStatus): void;
    },
    signal: AbortSignal,
  ): Promise<void>;
}
```

- [ ] **Step 1: Write replay/reconnect/gap tests**

Test last sequence handshake, replay/live overlap, duplicate/old event handling, committed-cursor persistence, heartbeat timeout, unknown event cursor advancement, 401/version fatal status, disconnect without cancel, bounded 250ms-to-10s backoff with fake timers, and gap snapshot sequence.

- [ ] **Step 2: Run failing stream tests**

Run: `npm test --workspace @incidentlens/cli -- src/stream`

- [ ] **Step 3: Implement WebSocket transport and reconnect policy**

Send bearer credential using the backend-approved handshake mechanism, client/schema versions, and `after_sequence`. Treat network/heartbeat loss as recoverable. Never send cancel on close.

- [ ] **Step 4: Implement gap recovery**

On gap/slow consumer: pause event projection; fetch Session, paginated messages, active Operation, and pending Approvals; replace the projection; set cursor to authoritative `last_event_sequence`; reconnect. Do not merge stale projection into the snapshot twice.

- [ ] **Step 5: Verify synchronization**

```bash
npm test --workspace @incidentlens/cli -- src/stream
npm run typecheck --workspace @incidentlens/cli
```

- [ ] **Step 6: Commit synchronization**

```bash
git add apps/cli/src/stream apps/cli/test/contract/ws-server.ts
git commit -m "feat(cli): recover Agent sessions after disconnects"
```

---

### Task 11: Render Safe Agent and Tool Progress

**Files:**
- Create: `apps/cli/src/ui/ToolCard.tsx`
- Create: `apps/cli/src/ui/ProgressItem.tsx`
- Create: `apps/cli/src/ui/InvestigationSummary.tsx`
- Test: `apps/cli/src/ui/ToolCard.test.tsx`
- Test: `apps/cli/src/ui/InvestigationSummary.test.tsx`

**Interfaces:**
- Consumes safe projection items from Task 6.
- Produces text/symbol/color redundant status cards for tool, Todo, hypothesis, Evidence, and child-task summaries.

- [ ] **Step 1: Write safe rendering tests**

Test proposed/running/succeeded/failed/uncertain updates in the same card, bounded summaries, Evidence IDs, Todo/Hypothesis/child states, `NO_COLOR`, and absence of raw args/output, logs, provider fields, credentials, and hidden reasoning.

- [ ] **Step 2: Run failing UI tests**

Run: `npm test --workspace @incidentlens/cli -- src/ui/ToolCard.test.tsx src/ui/InvestigationSummary.test.tsx`

- [ ] **Step 3: Implement concise progress components**

Use one-column conversation flow. Keep completed tools compact and expandable only for server-provided safe summaries. UNCERTAIN must be visually/textually distinct from ordinary failure and must not offer automatic retry.

- [ ] **Step 4: Verify safe UI**

```bash
npm test --workspace @incidentlens/cli -- src/ui/ToolCard.test.tsx src/ui/InvestigationSummary.test.tsx
npm run typecheck --workspace @incidentlens/cli
```

- [ ] **Step 5: Commit progress UI**

```bash
git add apps/cli/src/ui/ToolCard.tsx apps/cli/src/ui/ToolCard.test.tsx apps/cli/src/ui/ProgressItem.tsx apps/cli/src/ui/InvestigationSummary.tsx apps/cli/src/ui/InvestigationSummary.test.tsx
git commit -m "feat(cli): show safe Agent progress"
```

---

### Task 12: Implement CLI-Only Approval Interaction

**Files:**
- Create: `apps/cli/src/features/approvals/approval-controller.ts`
- Create: `apps/cli/src/features/approvals/approval-commands.ts`
- Create: `apps/cli/src/ui/ApprovalCard.tsx`
- Create: `apps/cli/src/ui/ApprovalReasonPrompt.tsx`
- Test: `apps/cli/src/features/approvals/approval-controller.test.ts`
- Test: `apps/cli/src/ui/ApprovalCard.test.tsx`

**Interfaces:**

```ts
export interface ApprovalController {
  refresh(id: string): Promise<ApprovalDetailView>;
  decide(id: string, decision: 'approve' | 'reject', reason: string): Promise<ApprovalDetailView>;
}
```

- [ ] **Step 1: Write approval safety tests**

Test pending event causes authoritative detail GET, safe diff/impact/verification/rollback/risk/expiry display, A/R/D only while focused and input empty, mandatory reason, expiration, duplicate decisions, persisted versus downstream status, cancel-before-approval, no optimistic success, and no canonical intent/secret rendering.

- [ ] **Step 2: Run failing approval tests**

Run: `npm test --workspace @incidentlens/cli -- src/features/approvals src/ui/ApprovalCard.test.tsx`

- [ ] **Step 3: Implement controller, commands, and scoped hotkeys**

`/approvals` lists pending approvals; `/approve`, `/reject`, `/diff` operate on selected/current Approval. A/R/D shortcuts are active only when the Approval card owns focus, prompt input is empty, and no palette/wizard/confirm overlay is active.

- [ ] **Step 4: Render two-stage outcomes**

Show “decision persisted” independently from “downstream processed/failed.” A failed downstream status is not rendered as a rejected or rolled-back decision.

- [ ] **Step 5: Verify approval flow**

```bash
npm test --workspace @incidentlens/cli -- src/features/approvals src/ui/ApprovalCard.test.tsx
npm run typecheck --workspace @incidentlens/cli
```

- [ ] **Step 6: Commit approval UI**

```bash
git add apps/cli/src/features/approvals apps/cli/src/ui/ApprovalCard.tsx apps/cli/src/ui/ApprovalReasonPrompt.tsx apps/cli/src/ui/ApprovalCard.test.tsx
git commit -m "feat(cli): handle exact operator approvals"
```

---

### Task 13: Integrate Bootstrap, Recovery, and Full Ink Flow

**Files:**
- Modify: `apps/cli/src/app/bootstrap.ts`
- Modify: `apps/cli/src/app/App.tsx`
- Modify: `apps/cli/src/cli.tsx`
- Create: `apps/cli/test/integration/cli-flow.test.tsx`

**Interfaces:**
- Bootstrap order: profile → token → compatibility → principal → target → Session snapshot → event stream.
- Produces one composed runtime from all prior tasks.

- [ ] **Step 1: Write the end-to-end component integration test**

Cover first startup/auth requirement, compatibility rejection, restored target/session, initial snapshot, WS from sequence, natural-language message, streaming text/tool updates, Approval, disconnect/gap recovery, `/clear`, `/cancel`, and `/exit` no cancel.

- [ ] **Step 2: Run failing integration test**

Run: `npm test --workspace @incidentlens/cli -- test/integration/cli-flow.test.tsx`

- [ ] **Step 3: Compose bootstrap and lifecycle**

Do not open WS before compatibility and authoritative snapshot. Persist sequence only after reducer application. Handle process signals through the same clean client shutdown path without canceling server work.

- [ ] **Step 4: Verify integrated Ink behavior**

```bash
npm test --workspace @incidentlens/cli -- test/integration/cli-flow.test.tsx
npm test --workspace @incidentlens/cli
npm run typecheck --workspace @incidentlens/cli
npm run build --workspace @incidentlens/cli
```

- [ ] **Step 5: Commit integration**

```bash
git add apps/cli/src/app apps/cli/src/cli.tsx apps/cli/test/integration/cli-flow.test.tsx
git commit -m "feat(cli): integrate durable cloud Agent workflow"
```

---

### Task 14: Verify the Real Terminal with PTY Tests

**Files:**
- Create: `apps/cli/test/pty/fake-control-plane.ts`
- Create: `apps/cli/test/pty/pty-driver.ts`
- Create: `apps/cli/test/pty/startup.pty.test.ts`
- Create: `apps/cli/test/pty/interaction.pty.test.ts`
- Create: `apps/cli/test/pty/reconnect.pty.test.ts`
- Create: `apps/cli/test/pty/approval.pty.test.ts`
- Modify: `apps/cli/package.json`

**Interfaces:**
- Runs the built executable through `node-pty`; does not render React directly.

- [ ] **Step 1: Build PTY driver and deterministic fake server**

The fake server implements version/auth/targets/sessions/operations/approvals and CLI WS, records cancellation calls, can force disconnect/backlog/gap, and never logs Authorization.

- [ ] **Step 2: Write terminal scenarios**

Cover 80×24 startup, resize to 120×40, Chinese wide characters, `/` arrows/Tab/Esc, natural-language send, Ctrl+C exit without cancel, `/cancel` with exactly one cancel call, forced reconnect/replay dedupe, gap snapshot, Approval A/R/D, stdin close, and output secret scan.

- [ ] **Step 3: Run real PTY tests**

```bash
npm run build --workspace @incidentlens/cli
npm run test:pty --workspace @incidentlens/cli
```

Expected: PASS locally on the current OS; CI matrix is added in Task 15.

- [ ] **Step 4: Commit PTY coverage**

```bash
git add apps/cli/test/pty apps/cli/package.json package-lock.json
git commit -m "test(cli): verify interactive terminal behavior"
```

---

### Task 15: Package and Verify npm Distribution

**Files:**
- Modify: `apps/cli/package.json`
- Modify: `apps/cli/tsup.config.ts`
- Create: `apps/cli/README.md`
- Create: `apps/cli/test/package/install-smoke.test.ts`
- Create: `.github/workflows/cli-ci.yml`
- Create: `.github/workflows/cli-release.yml`

**Interfaces:**
- Publishes `@incidentlens/cli`, bin `incidentlens`, public npm access, Node engine `>=22.19.0`.

- [ ] **Step 1: Write clean-prefix install tests**

Test tarball file allowlist, shebang/executable bin, no source/tests/.env/token fixtures, no unresolved private workspace dependency, `--version`, unsupported Node message, and missing keyring optional dependency yielding a safe authentication prompt rather than stack trace.

- [ ] **Step 2: Finalize package metadata**

Include only `dist`, README, and LICENSE. Bundle private protocol and CLI source; keep React/Ink/ws/keyring dependencies appropriate for npm installation. Keep node-pty dev-only. `prepack` runs protocol drift, typecheck, unit tests, and build.

- [ ] **Step 3: Add CI and release gates**

CI: protocol drift; unit/type/lint on Node 22.19/24/26; PTY on Ubuntu/macOS/Windows; package install smoke; existing Python pytest/Ruff. Release requires matching tag/package version and npm provenance. Do not publish from this implementation step unless separately authorized.

- [ ] **Step 4: Run the full CLI verification**

```bash
npm ci
npm run verify:cli
npm pack --workspace @incidentlens/cli --dry-run
npm pack --workspace @incidentlens/cli
npm test --workspace @incidentlens/cli -- test/package/install-smoke.test.ts
uv run pytest -q
uv run ruff check .
```

- [ ] **Step 5: Commit release readiness**

```bash
git add apps/cli/package.json apps/cli/tsup.config.ts apps/cli/README.md apps/cli/test/package .github/workflows/cli-ci.yml .github/workflows/cli-release.yml package-lock.json
git commit -m "build(cli): prepare verified npm distribution"
```

---

## CLI MVP Acceptance

Run:

```bash
npm ci
npm run protocol:check
npm run lint
npm run format:check
npm run typecheck
npm test
npm run build
npm run test:pty --workspace @incidentlens/cli
npm pack --workspace @incidentlens/cli --dry-run
uv run pytest -q
uv run ruff check .
```

The CLI phase is complete only when:

- A clean npm tarball installs and launches `incidentlens`.
- `/target` commands configure/select/test a server-side remote target without accepting private-key plaintext.
- Natural-language Chinese input creates/continues a server-side Agent Session with no local source prerequisite.
- Agent text, progress, tools, hypotheses, Evidence, and children render only safe summaries.
- CLI-only approvals show exact safe preview and distinguish decision persistence from downstream handling.
- `/cancel` cancels; Ctrl+C, `/exit`, stdin close, and network loss do not.
- Restart and WS reconnect recover from durable sequence without duplicates or silent holes.
- Gap/slow-consumer performs authoritative snapshot recovery.
- Real PTY tests pass for wide characters, resize, command palette, approval, and reconnect.
- The CLI contains no log dashboard, Web link, browser launcher, local SSH/model executor, or hidden reasoning.
