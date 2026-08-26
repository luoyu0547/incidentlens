/**
 * WebSocket event stream transport for IncidentLens CLI.
 *
 * Implements the `EventStream` contract on top of the `ws` package:
 * - Connects to `WS /ws/v1/cli-events` with the schema, session, and
 *   `after_sequence` query parameters.
 * - Authenticates via the `Authorization: Bearer <token>` header (the
 *   backend's `resolve_ws_principal` reads the bearer token from the
 *   handshake headers; Node's `ws` supports custom headers through the
 *   constructor options object).
 * - Waits for the `stream.hello` handshake. A `schema_version != 1`
 *   causes the server to close with 4406; unauthenticated closes 4401;
 *   missing read scope closes 4403. Close codes and hello payload are
 *   translated into `StreamStatus`.
 * - Parses every text frame with `parseStreamFrame`:
 *   - known envelopes are passed to `onEvent` in server sequence order;
 *   - unknown `event_type`s (newer server events) are safely skipped
 *     without blocking the cursor;
 *   - `stream.hello` / `stream.heartbeat` are consumed internally;
 *   - `stream.gap` and `stream.slow_consumer` surface an `onGap`.
 * - Treats network/heartbeat loss as recoverable (the synchronizer owns
 *   backoff and reconnect).
 * - Never sends `cancel` on close — cancel is only ever issued by the
 *   user through `/cancel`.
 *
 * Lifecycle:
 *   `connect()` resolves the returned promise once the `stream.hello`
 *   handshake frame is received (stream is live). If a fatal error
 *   (authentication, version mismatch) occurs before hello the promise
 *   rejects. After hello, the promise is settled and the handlers
 *   communicate all subsequent status. Callers abort the signal to
 *   stop the stream.
 */

import WebSocket from 'ws';
import { parseStreamFrame, ProtocolError } from '@incidentlens/protocol';
import type {
  EventStream,
  StreamCursor,
  StreamGap,
  StreamHandlers,
  StreamStatus,
} from './event-stream.js';

/**
 * Server close codes that must NOT be retried: authentication, read scope,
 * and protocol version negotiation failures.
 *
 * Notably 1012 (history pruned after `stream.gap`) and 1013 (slow
 * consumer) are RECOVERABLE — the synchronizer fetches an authoritative
 * HTTP snapshot and reconnects.
 */
const FATAL_CLOSE_CODES: ReadonlySet<number> = new Set([4401, 4403, 4406]);

/**
 * How long to wait (ms) for the `stream.hello` handshake after the
 * socket opens before considering the connection unhealthy.
 */
const HELLO_TIMEOUT_MS = 10_000;

/**
 * Build the WebSocket URL for a control plane API base URL.
 *
 * HTTP(S) URLs are translated to WS(S); the path is fixed for the CLI
 * event stream endpoint.
 */
export function buildCliEventsUrl(
  baseUrl: string,
  cursor: StreamCursor,
  filters?: {
    targetId?: string;
    investigationId?: string;
    eventType?: string;
  },
): string {
  const url = new URL(baseUrl);
  const proto = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.protocol = proto;
  url.pathname = '/ws/v1/cli-events';
  url.search = '';

  url.searchParams.set('schema_version', '1');
  url.searchParams.set('after_sequence', String(cursor.sequence));
  url.searchParams.set('session_id', cursor.sessionId);
  if (filters?.targetId) {
    url.searchParams.set('target_id', filters.targetId);
  }
  if (filters?.investigationId) {
    url.searchParams.set('investigation_id', filters.investigationId);
  }
  if (filters?.eventType) {
    url.searchParams.set('event_type', filters.eventType);
  }

  return url.toString();
}

/**
 * Extract `requested_after_sequence` / `earliest_available_sequence`
 * from a `stream.gap` payload. Missing fields stay undefined so the
 * synchronizer can fall back sanely.
 */
function parseGapPayload(
  payload: Record<string, unknown> | null | undefined,
): StreamGap {
  const numberOr = (value: unknown): number => {
    return typeof value === 'number' && Number.isFinite(value) ? value : 0;
  };
  return {
    requested_after_sequence: numberOr(payload?.requested_after_sequence),
    earliest_available_sequence: numberOr(payload?.earliest_available_sequence),
  };
}

/**
 * Concrete WebSocket event stream.
 *
 * Single-use: call `connect()`, then abort the signal to stop. The
 * promise resolves on hello and rejects on fatal errors before hello.
 * Reconnect and backoff are owned by the synchronizer.
 */
export class WsEventStream implements EventStream {
  private readonly baseUrl: string;
  private readonly token: string;

  constructor(config: { baseUrl: string; token: string }) {
    this.baseUrl = config.baseUrl;
    this.token = config.token;
  }

  async connect(
    cursor: StreamCursor,
    handlers: StreamHandlers,
    signal: AbortSignal,
  ): Promise<void> {
    if (signal.aborted) {
      return;
    }

    const url = buildCliEventsUrl(this.baseUrl, cursor);

    return new Promise<void>((resolve, reject) => {
      const socket = new WebSocket(url, {
        headers: { Authorization: `Bearer ${this.token}` },
        handshakeTimeout: HELLO_TIMEOUT_MS,
      });

      let helloSeen = false;
      let settled = false;

      // Removed from the shared signal on cleanup so repeated reconnects on
      // the same signal do not accumulate abort listeners.
      const onAbort = (): void => {
        cleanup();
        if (!settled) {
          settled = true;
          resolve();
        }
      };
      signal.addEventListener('abort', onAbort, { once: true });

      const cleanup = (): void => {
        try {
          signal.removeEventListener('abort', onAbort);
        } catch {
          // ignore
        }
        try {
          socket.removeAllListeners();
        } catch {
          // ignore
        }
        if (
          socket.readyState === WebSocket.OPEN ||
          socket.readyState === WebSocket.CONNECTING
        ) {
          try {
            socket.close(1000, 'client stop');
          } catch {
            // ignore
          }
        }
      };

      // --- Open ---
      socket.on('open', () => {
        // Connection established; wait for stream.hello.
      });

      // --- Message ---
      socket.on('message', (data: unknown, isBinary: boolean) => {
        if (isBinary) {
          return;
        }
        // Node `ws` delivers text frames as a Buffer by default; decode
        // them into the string the protocol parser expects.
        let raw: string | null = null;
        if (typeof data === 'string') {
          raw = data;
        } else if (Buffer.isBuffer(data)) {
          raw = data.toString('utf8');
        } else if (data instanceof ArrayBuffer) {
          raw = Buffer.from(data).toString('utf8');
        }
        if (raw === null) {
          return;
        }

        let parsed;
        try {
          parsed = parseStreamFrame(raw);
        } catch (error) {
          if (error instanceof ProtocolError) {
            handlers.onStatus({
              kind: 'fatal',
              error: error.message,
              code: error.code,
            });
            if (!helloSeen && !settled) {
              settled = true;
              reject(error);
            }
            cleanup();
            return;
          }
          handlers.onStatus({
            kind: 'fatal',
            error: 'Malformed stream frame',
          });
          if (!helloSeen && !settled) {
            settled = true;
            reject(new Error('Malformed stream frame'));
          }
          cleanup();
          return;
        }

        const envelope = parsed.envelope;

        // --- Stream control frames ---
        if (envelope.event_type === 'stream.hello') {
          if (!helloSeen) {
            helloSeen = true;
            // `parseStreamFrame` already rejects a non-1 schema_version
            // (UNSUPPORTED_SCHEMA_VERSION), so a successful parse here means
            // the stream is compatible. The transport is live.
            handlers.onStatus({ kind: 'connected' });
            if (!settled) {
              settled = true;
              resolve();
            }
          }
          return;
        }

        if (envelope.event_type === 'stream.heartbeat') {
          // Heartbeat received — connection is alive. Re-emit connected
          // so the synchronizer can reset its heartbeat timeout.
          handlers.onStatus({ kind: 'connected' });
          return;
        }

        if (envelope.event_type === 'stream.gap' || envelope.event_type === 'stream.slow_consumer') {
          const gap = parseGapPayload(envelope.payload ?? null);
          void handlers.onGap(gap);
          return;
        }

        // --- Runtime events ---
        if (typeof envelope.sequence === 'number') {
          if (parsed.kind === 'known') {
            void handlers.onEvent(parsed.envelope);
          }
          // Unknown event types advance the cursor safely (no onEvent call).
        }
      });

      // --- Error ---
      socket.on('error', (error: Error) => {
        if (!helloSeen && !settled) {
          // Handshake-phase network error — recoverable.
          settled = true;
          handlers.onStatus({ kind: 'recoverable', error: error.message });
          resolve();
          cleanup();
        } else {
          // Post-hello error — recoverable.
          handlers.onStatus({ kind: 'recoverable', error: error.message });
        }
      });

      // --- Close ---
      socket.on('close', (code: number, reason: Buffer) => {
        const reasonText = reason.toString();

        if (FATAL_CLOSE_CODES.has(code)) {
          handlers.onStatus({
            kind: 'fatal',
            error: reasonText || `Server closed with code ${code}`,
            code: String(code),
          });
          if (!helloSeen && !settled) {
            settled = true;
            reject(new Error(reasonText || `Server closed with code ${code}`));
          }
          cleanup();
          return;
        }

        const message = reasonText || `Connection closed with code ${code}`;
        handlers.onStatus({ kind: 'recoverable', error: message });

        if (!helloSeen && !settled) {
          // Closed before hello — this is still a recoverable outcome.
          settled = true;
          resolve();
          cleanup();
        }
      });
    });
  }
}