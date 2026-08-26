/**
 * Contract test WebSocket server.
 *
 * A tiny in-test `ws` server that emulates the backend
 * `WS /ws/v1/cli-events` surface for the CLI's stream transport tests:
 *
 * - Enforces `Authorization: Bearer <token>` (rejects with 4401 when the
 *   token is missing or wrong).
 * - Enforces `schema_version=1` (closes 4406 otherwise).
 * - Sends a `stream.hello` on connect, then replays the provided event
 *   list from `after_sequence` (with the same overlap semantics as the
 *   real server), then emits whatever the test drives.
 * - Can close immediately after accept with a given code/reason via
 *   `closeOnConnect` to simulate fatal negotiation failures.
 *
 * This is NOT the real control plane; it exists so the CLI's
 * `ws-event-stream` transport and the `session-synchronizer` can be
 * verified against the exact handshake/close codes and replay behavior
 * the backend guarantees.
 */

import { WebSocketServer } from 'ws';
import type { Server } from 'node:http';

export interface ContractEvent {
  sequence: number;
  event_type: string;
  session_id?: string | null;
  target_id?: string | null;
  payload?: Record<string, unknown> | null;
}

export interface ContractServerOptions {
  /** Expected bearer token; when set, a wrong/missing token closes 4401. */
  token?: string;
  /** Durable event log the server replays (oldest first). */
  events?: readonly ContractEvent[];
  /**
   * Schema version the server advertises. Defaults to 1. Setting a
   * different value closes 4406 in the hello payload.
   */
  schemaVersion?: number;
  /** When true, the server closes 4403 after hello (read scope missing). */
  rejectReadScope?: boolean;
  /**
   * Close the connection right after accept with this code/reason,
   * before sending hello. Simulates fatal negotiation failures.
   */
  closeOnConnect?: { code: number; reason: string };
  /** Called for every accepted connection with the request headers. */
  onHeaders?: (headers: Record<string, string>) => void;
  /** Called with the parsed `after_sequence` query param. */
  onAfterSequence?: (afterSequence: number) => void;
}

export interface ContractServerHandle {
  readonly url: string;
  close(): Promise<void>;
  /** Send a raw frame to all connected clients. */
  send(frame: Record<string, unknown>): void;
  /** Close all connections with a given code/reason. */
  closeConnections(code?: number, reason?: string): void;
  /** Abruptly terminate all connections (simulates a network drop). */
  terminateConnections(): void;
}

/**
 * Start a contract WS server on an existing http server.
 *
 * Each http server hosts at most one contract server; call `close()` to
 * detach the upgrade listener before attaching another to the same http
 * server.
 */
export function startContractServer(
  httpServer: Server,
  options: ContractServerOptions = {},
): Promise<ContractServerHandle> {
  const token = options.token;
  const events = [...(options.events ?? [])].sort((a, b) => a.sequence - b.sequence);
  const schemaVersion = options.schemaVersion ?? 1;

  return new Promise((resolve, reject) => {
    const wss = new WebSocketServer({ noServer: true });

    const onUpgrade = (request: unknown, socket: unknown, head: unknown): void => {
      wss.handleUpgrade(request, socket, head, (ws) => {
        wss.emit('connection', ws, request);
      });
    };
    httpServer.on('upgrade', onUpgrade);

    wss.on('connection', (socket, request) => {
      const req = request as { headers: Record<string, string | string[] | undefined>; url?: string };
      const headers = req.headers;
      const headerString: Record<string, string> = {};
      for (const [key, value] of Object.entries(headers)) {
        if (typeof value === 'string') {
          headerString[key] = value;
        }
      }
      options.onHeaders?.(headerString);

      const url = new URL(req.url ?? '/', 'http://localhost');
      const schema = Number(url.searchParams.get('schema_version') ?? '1');
      const afterSequence = Number(url.searchParams.get('after_sequence') ?? '0');
      options.onAfterSequence?.(afterSequence);

      // --- optional immediate close (simulates fatal negotiation) ---
      if (options.closeOnConnect) {
        socket.close(options.closeOnConnect.code, options.closeOnConnect.reason);
        return;
      }

      // --- schema_version negotiation ---
      if (schema !== 1 || schemaVersion !== 1) {
        socket.close(4406, 'unsupported schema version');
        return;
      }

      // --- authentication ---
      const authHeader = headerString['authorization'] ?? '';
      if (token && authHeader !== `Bearer ${token}`) {
        socket.close(4401, 'authentication required');
        return;
      }

      // --- read scope ---
      if (options.rejectReadScope) {
        socket.close(4403, 'read scope required');
        return;
      }

      // --- hello + replay ---
      socket.send(
        JSON.stringify({
          schema_version: 1,
          event_type: 'stream.hello',
          occurred_at: new Date().toISOString(),
        }),
      );

      // Replay events strictly after after_sequence, exactly like the
      // real server (500/page, overlap handled by sequence dedup on the
      // client).
      for (const event of events) {
        if (event.sequence > afterSequence) {
          socket.send(
            JSON.stringify({
              schema_version: 1,
              event_id: `evt-${event.sequence}`,
              sequence: event.sequence,
              event_type: event.event_type,
              session_id: event.session_id ?? null,
              target_id: event.target_id ?? null,
              occurred_at: new Date().toISOString(),
              payload: event.payload ?? null,
            }),
          );
        }
      }
    });

    const port = (httpServer.address() as { port: number }).port;
    const handle: ContractServerHandle = {
      url: `ws://127.0.0.1:${port}/ws/v1/cli-events`,
      close: () =>
        new Promise<void>((resolveClose) => {
          httpServer.removeListener('upgrade', onUpgrade);
          for (const client of wss.clients) {
            client.terminate();
          }
          wss.close(() => resolveClose());
        }),
      send: (frame) => {
        for (const client of wss.clients) {
          client.send(JSON.stringify(frame));
        }
      },
      closeConnections: (code = 1000, reason = '') => {
        for (const client of wss.clients) {
          client.close(code, reason);
        }
      },
      terminateConnections: () => {
        for (const client of wss.clients) {
          client.terminate();
        }
      },
    };

    resolve(handle);
  });
}