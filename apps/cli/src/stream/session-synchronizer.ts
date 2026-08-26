/**
 * Session synchronizer for IncidentLens CLI.
 *
 * Owns the recoverable WebSocket synchronization loop for a single Agent
 * session:
 *
 * - Connects the `EventStream` transport at the committed cursor
 *   (`lastSequenceBySession[sessionId]` from the profile).
 * - Advances the committed cursor on every applied event, and persists it
 *   to `ProfileConfig` so the session resumes exactly where the client
 *   stopped after a restart or disconnect.
 * - Treats network / heartbeat loss as recoverable: reconnect with bounded
 *   250 ms – 10 s exponential backoff (never sends cancel on close).
 * - On a history gap or slow consumer: pauses event projection, fetches an
 *   authoritative HTTP snapshot (Session + paginated messages + active
 *   Operation + pending Approvals), replaces the projection, sets the
 *   cursor to the server's authoritative `last_event_sequence`, then
 *   reconnects — strictly in that order. The stale projection is never
 *   merged into the snapshot.
 * - Marks 401 / version-incompatible as fatal (no reconnect) by surfacing
 *   `authentication-required` / `incompatible` to the host.
 * - Detects heartbeat timeout: when no `connected` status or data event
 *   arrives within `HEARTBEAT_TIMEOUT_MS`, the connection is treated as
 *   lost and a recoverable reconnect is triggered.
 *
 * Server-authority rules:
 * - The CLI never creates an Investigation or Run locally; the server's
 *   Session view, message list, and approval list are the source of truth.
 * - `/cancel` is the only path that cancels server work; disconnect and
 *   reconnect never do.
 */

import type { ControlPlaneApi } from '../api/control-plane-api.js';
import type { ConfigStore } from '../config/types.js';
import type { CliAction } from '../state/cli-state.js';
import type {
  KnownCliStreamEnvelope,
  StreamEventEnvelope,
  AgentMessageView,
  AgentSessionView,
  ApprovalDetailView,
  OperationView,
} from '@incidentlens/protocol';
import type {
  EventStream,
  StreamCursor,
  StreamGap,
  StreamStatus,
} from './event-stream.js';
import { backoffDelay } from './reconnect-policy.js';

/**
 * How long (ms) without a heartbeat or event before the connection is
 * considered dead and reconnected. Server heartbeats arrive every 15 s of
 * idle; any longer silence is a network/heartbeat loss.
 */
const HEARTBEAT_TIMEOUT_MS = 20_000;

/**
 * Message / event page size for authoritative snapshots during gap
 * recovery. The server's event log hard-caps page size at 500, so this
 * also bounds each `listEvents` request.
 */
const SNAPSHOT_PAGE_SIZE = 500;

/**
 * Operation lifecycle events that still represent a pending/non-terminal
 * operation. A terminal event (`operation.succeeded` / `.failed` /
 * `.cancelled`) means the operation is finished and must not be projected
 * as the session's active operation.
 */
const ACTIVE_OPERATION_EVENT_TYPES: ReadonlySet<string> = new Set([
  'operation.queued',
  'operation.running',
  'operation.cancel_requested',
  'operation.uncertain',
]);

/**
 * Operation statuses that are terminal.
 */
const TERMINAL_OPERATION_STATUSES: ReadonlySet<string> = new Set([
  'succeeded',
  'failed',
  'cancelled',
]);

/**
 * Projection built from authoritative HTTP data.
 */
export interface SessionSnapshot {
  readonly session: AgentSessionView;
  readonly messages: readonly AgentMessageView[];
  readonly operation: OperationView | null;
  readonly approvals: readonly ApprovalDetailView[];
  readonly lastSequence: number;
}

/**
 * Host callbacks the synchronizer drives.
 */
export interface SessionSynchronizerHost {
  /** Dispatch a `gap_snapshot` / `stream_event` / `set_stream_status` action. */
  dispatch: (action: CliAction) => void;
  /** Called when the committed cursor advances for a session. */
  onCursorAdvance?: (sessionId: string, sequence: number) => void;
}

/**
 * Options for constructing a session synchronizer.
 */
export interface SessionSynchronizerOptions {
  readonly api: ControlPlaneApi;
  readonly configStore: ConfigStore;
  readonly profileName: string;
  readonly eventStream: EventStream;
  readonly host: SessionSynchronizerHost;
  /**
   * Override the heartbeat timeout for tests (default 20 s).
   * Set to a small value (e.g. 50 ms) and use fake timers to test
   * heartbeat-loss recovery without waiting.
   */
  readonly heartbeatTimeoutMs?: number;
}

/**
 * Concrete session synchronizer.
 *
 * A synchronizer instance owns one session. `start` begins the loop; the
 * provided `signal` stops it. Reconnects are bounded by
 * `backoffDelay` (250 ms → 10 s), reset on a successful connect. Fatal
 * statuses stop the loop.
 */
export class SessionSynchronizer {
  private readonly api: ControlPlaneApi;
  private readonly configStore: ConfigStore;
  private readonly profileName: string;
  private readonly eventStream: EventStream;
  private readonly host: SessionSynchronizerHost;
  private readonly heartbeatTimeoutMs: number;

  /** Cursor persisted between reconnects. */
  private cursor: StreamCursor;
  /** Paused event projection while a gap snapshot is in flight. */
  private paused = false;

  constructor(options: SessionSynchronizerOptions) {
    this.api = options.api;
    this.configStore = options.configStore;
    this.profileName = options.profileName;
    this.eventStream = options.eventStream;
    this.host = options.host;
    this.heartbeatTimeoutMs = options.heartbeatTimeoutMs ?? HEARTBEAT_TIMEOUT_MS;
    this.cursor = { sessionId: '', sequence: 0 };
  }

  /**
   * Set the initial cursor from a committed `lastSequenceBySession` value.
   */
  setInitialCursor(sessionId: string, sequence: number): void {
    this.cursor = { sessionId, sequence };
  }

  /**
   * Run the synchronization loop for the configured session until the
   * signal is aborted or a fatal status is encountered.
   */
  async start(signal: AbortSignal): Promise<void> {
    if (signal.aborted) {
      return;
    }

    let attempt = 0;

    while (!signal.aborted) {
      const outcome = await this.runOnce(signal, {
        onConnected: () => {
          // A stable connection resets backoff so a later blip does not
          // wait out the full 10 s ceiling.
          attempt = 0;
        },
      });

      if (outcome.kind === 'fatal') {
        this.host.dispatch({
          type: 'set_stream_status',
          status: { connected: false, error: outcome.error },
        });
        return;
      }

      if (outcome.kind === 'stopped' || signal.aborted) {
        return;
      }

      // Recoverable — back off and retry.
      const delay = backoffDelay(attempt);
      attempt += 1;

      await this.sleep(delay, signal);
      if (signal.aborted) {
        return;
      }
    }
  }

  /**
   * Run one connection cycle: connect, pump events, return why it ended.
   *
   * `onConnected` is invoked when the transport reports a live connection
   * (hello / heartbeat), letting the host reset its backoff attempt count.
   */
  private async runOnce(
    signal: AbortSignal,
    hooks: { onConnected: () => void },
  ): Promise<
    | { kind: 'connected-and-closed' }
    | { kind: 'fatal'; error: string }
    | { kind: 'stopped' }
  > {
    return new Promise((resolve) => {
      let settled = false;
      let heartbeatTimer: ReturnType<typeof setTimeout> | undefined;
      // In-flight gap recovery, awaited before reconnecting so the cursor
      // is authoritative before the next connect.
      let gapRecovery: Promise<void> | null = null;

      const finalize = (
        outcome:
          | { kind: 'connected-and-closed' }
          | { kind: 'fatal'; error: string }
          | { kind: 'stopped' },
      ): void => {
        if (settled) {
          return;
        }
        settled = true;
        if (heartbeatTimer !== undefined) {
          clearTimeout(heartbeatTimer);
          heartbeatTimer = undefined;
        }
        signal.removeEventListener('abort', onAbort);
        resolve(outcome);
      };

      // The synchronizer owns its lifetime: aborting the signal stops the
      // loop even while the transport is connected and idle (a transport
      // only reports close/abort events while its promise is pending).
      const onAbort = (): void => {
        finalize({ kind: 'stopped' });
      };
      signal.addEventListener('abort', onAbort, { once: true });

      const resetHeartbeat = (): void => {
        if (heartbeatTimer !== undefined) {
          clearTimeout(heartbeatTimer);
        }
        heartbeatTimer = setTimeout(() => {
          // Heartbeat timeout — treat as recoverable disconnect.
          finalize({ kind: 'connected-and-closed' });
          this.host.dispatch({
            type: 'set_stream_status',
            status: { connected: false, error: 'Heartbeat timeout' },
          });
        }, this.heartbeatTimeoutMs);
      };

      const finishRecoverable = (error: string): void => {
        finalize({ kind: 'connected-and-closed' });
        this.host.dispatch({
          type: 'set_stream_status',
          status: { connected: false, error },
        });
      };

      const handlers = {
        onEvent: (event: KnownCliStreamEnvelope): void => {
          if (this.paused) {
            return;
          }
          if (typeof event.sequence !== 'number') {
            return;
          }
          if (event.sequence <= this.cursor.sequence) {
            // Old/duplicate — the reducer also guards this, but skip
            // early so the cursor is never moved backwards.
            return;
          }
          // Flatten payload into the top-level event for the reducer.
          this.host.dispatch({
            type: 'stream_event',
            event: { ...event, ...(event.payload ?? {}) },
          });
          this.cursor = { sessionId: this.cursor.sessionId, sequence: event.sequence };
          this.host.onCursorAdvance?.(this.cursor.sessionId, event.sequence);
          void this.persistCursor();
          resetHeartbeat();
        },

        onGap: async (gap: StreamGap): Promise<void> => {
          if (this.paused) {
            return;
          }
          this.paused = true;
          this.host.dispatch({
            type: 'set_stream_status',
            status: { connected: false, error: 'Event history gap — resynchronizing' },
          });
          const recovery = this.recoverFromGap(gap).finally(() => {
            this.paused = false;
          });
          gapRecovery = recovery;
          try {
            await recovery;
          } catch (error) {
            const message = error instanceof Error ? error.message : 'Gap recovery failed';
            this.host.dispatch({
              type: 'set_stream_status',
              status: { connected: false, error: message },
            });
          }
        },

        onStatus: (status: StreamStatus): void => {
          switch (status.kind) {
            case 'connected':
              this.host.dispatch({
                type: 'set_stream_status',
                status: { connected: true, error: undefined },
              });
              hooks.onConnected();
              resetHeartbeat();
              break;
            case 'recoverable':
              // A gap closes with 1012 right after the `stream.gap` frame.
              // Wait for any in-flight gap recovery so the cursor is set to
              // the authoritative sequence BEFORE the reconnect, otherwise
              // the loop would re-connect at the old cursor and re-apply
              // events the snapshot already captured (double merge).
              if (gapRecovery !== null) {
                void gapRecovery.catch(() => undefined).then(() => {
                  gapRecovery = null;
                  finishRecoverable(status.error);
                });
              } else {
                finishRecoverable(status.error);
              }
              break;
            case 'fatal':
              finalize({ kind: 'fatal', error: status.error });
              this.host.dispatch({
                type: 'set_stream_status',
                status: { connected: false, error: status.error },
              });
              break;
          }
        },
      };

      void this.eventStream.connect(this.cursor, handlers, signal).then(
        () => {
          // connect resolved after hello (or immediately on abort). Keep
          // pumping; closure of the socket is signaled via onStatus or the
          // abort listener.
        },
        (error) => {
          // connect rejected with a fatal error.
          const message = error instanceof Error ? error.message : 'Stream connect failed';
          finalize({ kind: 'fatal', error: message });
          this.host.dispatch({
            type: 'set_stream_status',
            status: { connected: false, error: message },
          });
        },
      );
    });
  }

  /**
   * Recover from a history gap or slow consumer.
   *
   * Fetches the authoritative HTTP snapshot, surfaces the session, replaces
   * the projection via `gap_snapshot`, and sets the cursor to the server's
   * authoritative latest sequence. The caller then reconnects at that
   * cursor. The stale projection is never merged into the snapshot.
   */
  private async recoverFromGap(gap: StreamGap): Promise<void> {
    const sessionId = this.cursor.sessionId;

    const snapshot = await this.fetchAuthoritativeSnapshot(sessionId);

    // Surface the authoritative session view (the reducer's gap_snapshot
    // does not carry a session field).
    this.host.dispatch({ type: 'set_session', session: snapshot.session });

    // Replace the projection with the authoritative snapshot. The stale
    // projection is discarded, never merged.
    this.host.dispatch({
      type: 'gap_snapshot',
      snapshot: {
        messages: snapshot.messages.map((m) => ({
          kind: 'text' as const,
          messageId: m.message_id,
          blockId: m.message_id,
          content: m.content,
        })),
        operations: snapshot.operation
          ? { [snapshot.operation.operation_id]: snapshot.operation }
          : {},
        approvals: Object.fromEntries(
          snapshot.approvals.map((a) => [a.approval_id, a]),
        ),
        sequence: snapshot.lastSequence,
      },
    });

    // Cursor becomes the authoritative last event sequence.
    this.cursor = { sessionId, sequence: snapshot.lastSequence };
    await this.persistCursor();
  }

  /**
   * Fetch the authoritative HTTP snapshot for a session.
   */
  private async fetchAuthoritativeSnapshot(
    sessionId: string,
  ): Promise<SessionSnapshot> {
    const [session, messages, approvals, events] = await Promise.all([
      this.api.getSession(sessionId),
      this.fetchAllMessages(sessionId),
      this.fetchApprovals(sessionId),
      this.fetchAllEvents(sessionId),
    ]);

    return {
      session,
      messages,
      operation: await this.resolveActiveOperation(events),
      approvals,
      lastSequence: this.computeAuthoritativeSequence(sessionId, events),
    };
  }

  /**
   * Compute the authoritative last sequence for a session from its event
   * log. We take the max sequence of events strictly after the committed
   * cursor (never the global `latest_sequence`, which is unfiltered).
   * Fall back to the committed cursor when the session has no newer events.
   */
  private computeAuthoritativeSequence(
    sessionId: string,
    events: readonly StreamEventEnvelope[],
  ): number {
    let lastSequence = this.cursor.sequence;
    for (const event of events) {
      if (
        typeof event.sequence === 'number' &&
        event.sequence > lastSequence
      ) {
        lastSequence = event.sequence;
      }
    }
    return lastSequence;
  }

  /**
   * Fetch all messages for a session, paginated.
   */
  private async fetchAllMessages(sessionId: string): Promise<AgentMessageView[]> {
    const messages: AgentMessageView[] = [];
    let offset = 0;
    for (;;) {
      const page = await this.api.listMessages(sessionId, {
        limit: SNAPSHOT_PAGE_SIZE,
        offset,
      });
      messages.push(...page);
      if (page.length < SNAPSHOT_PAGE_SIZE) {
        break;
      }
      offset += page.length;
    }
    return messages;
  }

  /**
   * Fetch the session's event log, paginating until `has_more` is false so
   * the authoritative cursor is not underestimated (which would re-apply
   * events already captured by the snapshot).
   */
  private async fetchAllEvents(sessionId: string): Promise<StreamEventEnvelope[]> {
    const events: StreamEventEnvelope[] = [];
    let afterSequence = this.cursor.sequence;
    for (;;) {
      const page = await this.api.listEvents({
        sessionId,
        afterSequence,
        limit: SNAPSHOT_PAGE_SIZE,
      });
      events.push(...page.items);
      if (!page.has_more || page.items.length === 0) {
        break;
      }
      afterSequence = page.next_after_sequence;
    }
    return events;
  }

  /**
   * Resolve the current active (non-terminal) operation for a session from
   * its event log.
   *
   * Terminal operation events (`operation.succeeded` / `.failed` /
   * `.cancelled`) mark a finished operation and are excluded; the most
   * recent non-terminal operation event is resolved through the
   * authoritative `getOperation` read surface. We also re-check the
   * operation status so a stale in-flight event does not project a
   * finished operation as active. When nothing is active we return null.
   */
  private async resolveActiveOperation(
    events: readonly StreamEventEnvelope[],
  ): Promise<OperationView | null> {
    const operationEvents = events
      .filter((e) => ACTIVE_OPERATION_EVENT_TYPES.has(e.event_type))
      .sort((a, b) => (b.sequence ?? 0) - (a.sequence ?? 0));

    for (const event of operationEvents) {
      const operationId = event.payload?.['operation_id'];
      if (typeof operationId !== 'string' || operationId.length === 0) {
        continue;
      }
      try {
        const operation = await this.api.getOperation(operationId);
        if (operation && !TERMINAL_OPERATION_STATUSES.has(operation.status)) {
          return operation;
        }
      } catch {
        // Operation lookup failed — try the next candidate.
      }
    }
    return null;
  }

  /**
   * Fetch pending approvals for the session.
   */
  private async fetchApprovals(sessionId: string): Promise<ApprovalDetailView[]> {
    const page = await this.api.listApprovals({
      sessionId,
      status: 'pending',
      limit: SNAPSHOT_PAGE_SIZE,
    });
    return page.items;
  }

  /**
   * Persist the committed cursor to the profile.
   */
  private async persistCursor(): Promise<void> {
    try {
      const profile = await this.configStore.load(this.profileName);
      if (!profile) {
        return;
      }
      const lastSequenceBySession = {
        ...profile.lastSequenceBySession,
        [this.cursor.sessionId]: this.cursor.sequence,
      };
      await this.configStore.save({ ...profile, lastSequenceBySession });
    } catch {
      // Persistence failure is non-fatal; cursor advances in memory.
    }
  }

  /**
   * Sleep with abort support. Removes its abort listener once the timer
   * fires so repeated backoffs on a shared signal do not accumulate
   * listeners.
   */
  private sleep(ms: number, signal: AbortSignal): Promise<void> {
    return new Promise((resolve) => {
      const onAbort = (): void => {
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        signal.removeEventListener('abort', onAbort);
        resolve();
      }, ms);
      signal.addEventListener('abort', onAbort, { once: true });
    });
  }
}