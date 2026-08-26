/**
 * Session controller for IncidentLens CLI.
 *
 * Wraps the control plane Agent Session surface and the profile config
 * store. The controller is the single dependency the command layer and
 * the natural-language submit path use to list, create, select, resume,
 * rename, and cancel remote Agent sessions.
 *
 * Server-authoritative orchestration rules:
 * - No Investigation/Run is ever created locally. All session lifecycle
 *   state (session_id, message_id, operation_id) comes from the Agent
 *   Session endpoints.
 * - `sendNaturalLanguage` requires a selected target. If no session is
 *   active yet it creates one (create-on-first-message); subsequent
 *   messages reuse that session. A pending idempotency key is stored so
 *   an interrupted message can be retried with the same key.
 * - `/cancel` is the ONLY path that calls the cancel API. `/exit`,
 *   Ctrl+C, stdin close, and WS disconnect never cancel server work.
 * - `/resume` attaches to a session and requests server-side recovery;
 *   it never fabricates state locally.
 *
 * Security rules:
 * - Only server-redacted HTTP DTOs from `@incidentlens/protocol` are
 *   forwarded to callers. Raw tool args/output, provider payloads,
 *   hidden reasoning, unsanitized logs, and canonical intents never
 *   reach this layer or the UI.
 */

import { createIdempotencyKey } from '../../api/idempotency.js';
import type { ControlPlaneApi } from '../../api/control-plane-api.js';
import type { ConfigStore } from '../../config/types.js';
import type { CliAction } from '../../state/cli-state.js';
import type {
  AgentMessageAccepted,
  AgentSessionView,
  OperationAccepted,
  OperationView,
  TargetView,
} from '@incidentlens/protocol';

/**
 * Session controller interface.
 *
 * The minimal contract consumed by command handlers and the submit path.
 */
export interface SessionController {
  sendNaturalLanguage(text: string): Promise<AgentMessageAccepted>;
  create(title?: string): Promise<AgentSessionView>;
  select(session: AgentSessionView): Promise<void>;
  get(sessionId: string): Promise<AgentSessionView>;
  list(): Promise<readonly AgentSessionView[]>;
  resume(sessionId: string): Promise<OperationAccepted>;
  cancelCurrent(): Promise<OperationView>;
  rename(title: string): Promise<AgentSessionView>;
}

/**
 * Options for constructing a session controller.
 */
export interface SessionControllerOptions {
  readonly api: ControlPlaneApi;
  readonly configStore: ConfigStore;
  readonly profileName: string;
  readonly dispatch?: (action: CliAction) => void;
}

/**
 * Pending natural-language state.
 *
 * When the first send on a fresh session is interrupted after the
 * session was created but before the message was acked, the CLI keeps
 * this record so `sendNaturalLanguage` resolves the same session_id and
 * reuses the same idempotency key on the retry (deduplicated server-side).
 */
interface PendingSend {
  readonly sessionId: string;
  readonly idempotencyKey: string;
}

/**
 * Active session/target projection mirrored from the reducer so the
 * controller always operates on the same state the UI renders.
 */
interface ActiveProjection {
  readonly target: TargetView | undefined;
  readonly session: AgentSessionView | undefined;
}

/**
 * Concrete session controller.
 *
 * Tracks the current target and session via `sync()`, which the App
 * calls on every render. `sendNaturalLanguage` uses these to implement
 * no-target blocking and create-on-first-message.
 */
export class SessionController implements SessionController {
  private readonly api: ControlPlaneApi;
  private readonly configStore: ConfigStore;
  private readonly profileName: string;
  private readonly dispatch?: (action: CliAction) => void;

  /** Current active projection (target + session). */
  private active: ActiveProjection = { target: undefined, session: undefined };

  /** Pending send awaiting retry (single-flight per controller). */
  private pendingSend: PendingSend | undefined;

  constructor(options: SessionControllerOptions) {
    this.api = options.api;
    this.configStore = options.configStore;
    this.profileName = options.profileName;
    this.dispatch = options.dispatch;
  }

  /**
   * Sync the controller's view of the active target and session.
   * Called by the App on every render so the controller always has an
   * up-to-date reference without needing to subscribe to the reducer.
   */
  sync(target: TargetView | undefined, session: AgentSessionView | undefined): void {
    this.active = { target, session };
  }

  /**
   * Persist the current profile's last session selection.
   */
  private async persistLastSession(session: AgentSessionView): Promise<void> {
    const existing = await this.configStore.load(this.profileName);
    if (existing) {
      await this.configStore.save({ ...existing, lastSessionId: session.session_id });
    }
  }

  /**
   * Send a natural-language message.
   *
   * - No-target blocking: throws when no target is selected.
   * - create-on-first-message: when no session matches the current target
   *   a new session is created first, then the message is enqueued.
   * - Once a send succeeds the created/passed session becomes active.
   * - Interrupted sends retain the idempotency key for retry.
   */
  async sendNaturalLanguage(text: string): Promise<AgentMessageAccepted> {
    if (!this.active.target) {
      throw new Error('No target selected. Use /target <name> first.');
    }

    const session = await this.resolveSession();

    const idempotencyKey =
      this.pendingSend?.sessionId === session.session_id
        ? this.pendingSend.idempotencyKey
        : createIdempotencyKey();

    try {
      const accepted = await this.api.sendMessage(
        session.session_id,
        { content: text },
        { idempotencyKey },
      );
      this.pendingSend = undefined;
      return accepted;
    } catch (error) {
      // Keep the pending key so the caller can retry with the same key.
      this.pendingSend = { sessionId: session.session_id, idempotencyKey };
      throw error;
    }
  }

  /**
   * Resolve which session a natural-language message targets.
   *
   * Reuses a pending session (create-on-first-message interrupted after
   * the create), otherwise the active session when it matches the target,
   * otherwise a freshly created session pinned to the target.
   */
  private async resolveSession(): Promise<AgentSessionView> {
    const { target, session } = this.active;

    if (this.pendingSend) {
      // Prefer the active session when it is the pending one; otherwise
      // refetch from the server to recover a session created by an
      // interrupted first message.
      if (session && session.session_id === this.pendingSend.sessionId) {
        return session;
      }
      return this.api.getSession(this.pendingSend.sessionId);
    }

    if (target && session && session.target_id === target.target_id) {
      return session;
    }

    if (!target) {
      throw new Error('No target selected. Use /target <name> first.');
    }

    const created = await this.api.createSession(
      { target_id: target.target_id },
      { idempotencyKey: createIdempotencyKey() },
    );
    this.dispatch?.({ type: 'set_session', session: created });
    this.active = { target, session: created };
    await this.persistLastSession(created);
    return created;
  }

  /**
   * Create a new session pinned to the current target.
   */
  async create(title?: string): Promise<AgentSessionView> {
    if (!this.active.target) {
      throw new Error('No target selected. Use /target <name> first.');
    }

    const session = await this.api.createSession(
      { target_id: this.active.target.target_id, title: title ?? null },
      { idempotencyKey: createIdempotencyKey() },
    );
    this.dispatch?.({ type: 'set_session', session });
    this.active = { target: this.active.target, session };
    await this.persistLastSession(session);
    return session;
  }

  /**
   * Select a session as the active one and persist the choice.
   */
  async select(session: AgentSessionView): Promise<void> {
    this.dispatch?.({ type: 'set_session', session });
    this.active = { target: this.active.target, session };
    await this.persistLastSession(session);
  }

  /**
   * List all sessions from the control plane.
   */
  async list(): Promise<readonly AgentSessionView[]> {
    return this.api.listSessions({}, undefined);
  }

  /**
   * Fetch a single session by id.
   */
  async get(sessionId: string): Promise<AgentSessionView> {
    return this.api.getSession(sessionId);
  }

  /**
   * Resume a session: attach and ask the server to recover execution.
   * The server returns an Operation to follow; `/cancel` is the only
   * path that requests cancellation.
   */
  async resume(sessionId: string): Promise<OperationAccepted> {
    const accepted = await this.api.resumeSession(sessionId, {
      idempotencyKey: createIdempotencyKey(),
    });
    const session = await this.api.getSession(sessionId);
    this.dispatch?.({ type: 'set_session', session });
    this.active = { target: this.active.target, session };
    await this.persistLastSession(session);
    return accepted;
  }

  /**
   * Cancel the in-flight operation of a specific session.
   * This is the only path that calls the cancel API; application
   * shutdown never does.
   */
  async cancel(sessionId: string): Promise<OperationView> {
    return this.api.cancelSession(sessionId, {
      idempotencyKey: createIdempotencyKey(),
    });
  }

  /**
   * Cancel the active session's in-flight operation.
   */
  async cancelCurrent(): Promise<OperationView> {
    if (!this.active.session) {
      throw new Error('No active session to cancel');
    }
    return this.cancel(this.active.session.session_id);
  }

  /**
   * Rename the active session (title only, via PATCH).
   */
  async rename(title: string): Promise<AgentSessionView> {
    if (!this.active.session) {
      throw new Error('No active session to rename');
    }
    const updated = await this.api.patchSession(
      this.active.session.session_id,
      { title },
      { idempotencyKey: createIdempotencyKey() },
    );
    this.dispatch?.({ type: 'set_session', session: updated });
    this.active = { target: this.active.target, session: updated };
    return updated;
  }

  /**
   * Return the current active session, if any.
   */
  getCurrent(): AgentSessionView | undefined {
    return this.active.session;
  }
}