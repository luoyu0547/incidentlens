/**
 * Event stream contracts for IncidentLens CLI.
 *
 * Shared types for the WebSocket synchronization layer (Task 10):
 * - `StreamCursor` addresses a session and a resume sequence.
 * - `EventStream` is the transport contract the App and bootstrap rely
 *   on. It is intentionally minimal: connect, stream events by `sequence`,
 *   surface gaps, and report connection status.
 * - `StreamStatus`/`StreamHealth` distinguish recoverable (retry) from
 *   fatal (do-not-retry) conditions so the synchronizer can back off and
 *   retry network/heartbeat loss without ever reconnecting on 401 or a
 *   version mismatch.
 *
 * Server-authority rules:
 * - The server assigns every event a monotonically increasing `sequence`.
 * - Events with `sequence <=` the committed cursor are old/duplicate and
 *   are rejected by the reducer; the transport never skips them.
 * - `stream.gap` / `stream.slow_consumer` mean event history for the
 *   requested cursor was truncated or the consumer fell behind. The client
 *   must fetch an authoritative HTTP snapshot and resume from the server's
 *   `last_event_sequence` instead of merging stale projection state twice.
 */

import type { KnownCliStreamEnvelope } from '@incidentlens/protocol';

/**
 * A server-assigned event cursor for a single session.
 *
 * `sequence` is the last committed server sequence for `sessionId`. The
 * transport requests replay starting strictly after this value.
 */
export interface StreamCursor {
  readonly sessionId: string;
  readonly sequence: number;
}

/**
 * The `stream.gap` payload, projected from the server envelope.
 *
 * The server hands us the cursor we requested (`requested_after_sequence`)
 * and the earliest sequence still retained on the server
 * (`earliest_available_sequence`). When the requested cursor is below that
 * watermark the event log has been truncated and a full HTTP snapshot is
 * required.
 */
export interface StreamGap {
  readonly requested_after_sequence: number;
  readonly earliest_available_sequence: number;
}

/**
 * Connection status reported by the transport.
 *
 * `kind: 'recoverable'` means the socket should be retried with backoff.
 * `kind: 'fatal'` means retrying is pointless (authentication failure,
 * protocol version mismatch) and the host should mark the session
 * `incompatible` / `authentication-required`.
 */
export type StreamStatus =
  | { kind: 'connected' }
  | { kind: 'recoverable'; error: string }
  | { kind: 'fatal'; error: string; code?: string };

/**
 * Handlers the transport calls as frames arrive.
 */
export interface StreamHandlers {
  /** Called for every parsed known envelope, in server sequence order. */
  onEvent(event: KnownCliStreamEnvelope): Promise<void> | void;
  /** Called when the server reports a history gap (or slow consumer). */
  onGap(gap: StreamGap): Promise<void>;
  /** Called with transport-level status changes. */
  onStatus(status: StreamStatus): void;
}

/**
 * WebSocket transport contract.
 *
 * `connect` resolves once a connection has been established and the
 * initial `stream.hello` has been parsed (or rejects with a fatal error).
 * The returned promise stays pending for the life of the stream and
 * resolves when the caller's `signal` is aborted.
 */
export interface EventStream {
  connect(
    cursor: StreamCursor,
    handlers: StreamHandlers,
    signal: AbortSignal,
  ): Promise<void>;
}