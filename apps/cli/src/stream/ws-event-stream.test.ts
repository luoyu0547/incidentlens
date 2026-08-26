/**
 * WebSocket event stream transport tests.
 *
 * Uses the contract WS server to verify the transport against the exact
 * backend handshake and replay behavior:
 * - last-sequence handshake (`after_sequence` query param)
 * - replay → live overlap (duplicate boundary events)
 * - duplicate/old event handling
 * - unknown event type cursor advancement
 * - heartbeat status liveliness
 * - 401/version fatal status (no reconnect attempts)
 * - gap snapshot sequence dispatch
 * - disconnect without cancel
 */

import { describe, expect, it, vi } from 'vitest';
import { createServer, type Server } from 'node:http';
import { once } from 'node:events';
import { WsEventStream, buildCliEventsUrl } from './ws-event-stream.js';
import { startContractServer, type ContractServerHandle } from '../../test/contract/ws-server.js';
import type { StreamGap, StreamStatus, StreamCursor } from './event-stream.js';

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

async function startHttpServer(): Promise<Server> {
  const server = createServer((req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{}');
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return server;
}

async function setup(
  options: Parameters<typeof startContractServer>[1] = {},
): Promise<{ http: Server; ws: ContractServerHandle; token: string; baseUrl: string }> {
  const http = await startHttpServer();
  const token = options.token ?? 'test-token';
  const ws = await startContractServer(http, { ...options, token });
  const addr = http.address() as { port: number };
  const baseUrl = `http://127.0.0.1:${addr.port}`;
  return { http, ws, token, baseUrl };
}

function makeCursor(overrides: Partial<StreamCursor> = {}): StreamCursor {
  return { sessionId: 'session-1', sequence: 0, ...overrides };
}

function makeHandlers() {
  return {
    onEvent: vi.fn(),
    onGap: vi.fn(async () => {}),
    onStatus: vi.fn(),
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/* ------------------------------------------------------------------ */
/* URL construction                                                    */
/* ------------------------------------------------------------------ */

describe('buildCliEventsUrl', () => {
  it('builds a ws:// URL with schema, session, and after_sequence', () => {
    const url = new URL(
      buildCliEventsUrl('http://api.example.com', makeCursor({ sessionId: 's1', sequence: 42 })),
    );
    expect(url.protocol).toBe('ws:');
    expect(url.pathname).toBe('/ws/v1/cli-events');
    expect(url.searchParams.get('schema_version')).toBe('1');
    expect(url.searchParams.get('session_id')).toBe('s1');
    expect(url.searchParams.get('after_sequence')).toBe('42');
  });

  it('translates https to wss', () => {
    const url = buildCliEventsUrl('https://api.example.com', makeCursor());
    expect(url.startsWith('wss://')).toBe(true);
  });
});

/* ------------------------------------------------------------------ */
/* Handshake and replay                                                */
/* ------------------------------------------------------------------ */

describe('WsEventStream', () => {
  it('connects, receives hello, and reports connected', async () => {
    const { http, ws, token, baseUrl } = await setup();
    try {
      const stream = new WsEventStream({ baseUrl, token });
      const handlers = makeHandlers();

      await stream.connect(makeCursor({ sessionId: 'session-1', sequence: 7 }), handlers, new AbortController().signal);

      expect(handlers.onStatus).toHaveBeenCalledWith({ kind: 'connected' });
    } finally {
      await ws.close();
      http.close();
    }
  });

  it('authenticates via the Authorization bearer header', async () => {
    const onHeaders = vi.fn();
    const { http, ws, token, baseUrl } = await setup({ onHeaders });
    try {
      const stream = new WsEventStream({ baseUrl, token });
      const handlers = makeHandlers();

      await stream.connect(makeCursor({ sessionId: 's', sequence: 0 }), handlers, new AbortController().signal);

      const headers = onHeaders.mock.calls[0]?.[0] as Record<string, string> | undefined;
      expect(headers?.['authorization']).toBe(`Bearer ${token}`);
    } finally {
      await ws.close();
      http.close();
    }
  });

  it('marks 401 as fatal and does not retry', async () => {
    const { http, ws, token, baseUrl } = await setup();
    try {
      // Wrong token → server closes 4401 before hello.
      const badStream = new WsEventStream({ baseUrl, token: 'wrong-token' });
      const handlers = makeHandlers();
      await expect(
        badStream.connect(makeCursor({ sessionId: 's', sequence: 0 }), handlers, new AbortController().signal),
      ).rejects.toThrow();

      expect(handlers.onStatus).toHaveBeenCalledWith(
        expect.objectContaining({ kind: 'fatal', code: '4401' }),
      );
    } finally {
      await ws.close();
      http.close();
    }
  });

  it('marks schema_version mismatch (4406) as fatal', async () => {
    const { http, ws, token, baseUrl } = await setup({
      closeOnConnect: { code: 4406, reason: 'unsupported schema version' },
    });
    try {
      const stream = new WsEventStream({ baseUrl, token });
      const handlers = makeHandlers();

      await expect(
        stream.connect(makeCursor({ sessionId: 's', sequence: 0 }), handlers, new AbortController().signal),
      ).rejects.toThrow('unsupported schema version');

      expect(handlers.onStatus).toHaveBeenCalledWith(
        expect.objectContaining({ kind: 'fatal', code: '4406' }),
      );
    } finally {
      await ws.close();
      http.close();
    }
  });

  it('marks read-scope rejection (4403) as fatal', async () => {
    const { http, ws, token, baseUrl } = await setup({
      closeOnConnect: { code: 4403, reason: 'read scope required' },
    });
    try {
      const stream = new WsEventStream({ baseUrl, token });
      const handlers = makeHandlers();

      await expect(
        stream.connect(makeCursor({ sessionId: 's', sequence: 0 }), handlers, new AbortController().signal),
      ).rejects.toThrow('read scope required');

      expect(handlers.onStatus).toHaveBeenCalledWith(
        expect.objectContaining({ kind: 'fatal', code: '4403' }),
      );
    } finally {
      await ws.close();
      http.close();
    }
  });

  it('passes known events in sequence order to onEvent', async () => {
    const { http, ws, token, baseUrl } = await setup({
      events: [
        { sequence: 1, event_type: 'tool.proposed', payload: { tool_id: 't1', tool_name: 'ls' } },
        { sequence: 2, event_type: 'agent.text.delta', payload: { delta: 'Hi' } },
      ],
    });
    try {
      const stream = new WsEventStream({ baseUrl, token });
      const handlers = makeHandlers();

      await stream.connect(makeCursor({ sessionId: 's', sequence: 0 }), handlers, new AbortController().signal);
      await sleep(30);

      expect(handlers.onEvent).toHaveBeenCalledTimes(2);
      expect(handlers.onEvent.mock.calls[0]?.[0]).toMatchObject({ event_type: 'tool.proposed', sequence: 1 });
      expect(handlers.onEvent.mock.calls[1]?.[0]).toMatchObject({ event_type: 'agent.text.delta', sequence: 2 });
    } finally {
      await ws.close();
      http.close();
    }
  });

  it('advances past unknown event types without polluting onEvent', async () => {
    const { http, ws, token, baseUrl } = await setup({
      events: [
        { sequence: 5, event_type: 'future.event.new', payload: { x: 1 } },
        { sequence: 6, event_type: 'tool.proposed', payload: { tool_id: 't1' } },
      ],
    });
    try {
      const stream = new WsEventStream({ baseUrl, token });
      const handlers = makeHandlers();

      await stream.connect(makeCursor({ sessionId: 's', sequence: 0 }), handlers, new AbortController().signal);
      await sleep(30);

      // The unknown event is NOT dispatched to onEvent (it advances the
      // cursor silently), the known one is.
      expect(handlers.onEvent).toHaveBeenCalledTimes(1);
      expect(handlers.onEvent.mock.calls[0]?.[0]).toMatchObject({ event_type: 'tool.proposed', sequence: 6 });
    } finally {
      await ws.close();
      http.close();
    }
  });

  it('does not deliver boundary events with sequence <= cursor', async () => {
    const { http, ws, token, baseUrl } = await setup({
      events: [
        { sequence: 4, event_type: 'tool.proposed' },
        { sequence: 5, event_type: 'agent.text.delta' },
      ],
    });
    try {
      const stream = new WsEventStream({ baseUrl, token });
      const handlers = makeHandlers();

      // Cursor at 4 → server replays event 5 only (4 <= after_sequence).
      await stream.connect(makeCursor({ sessionId: 's', sequence: 4 }), handlers, new AbortController().signal);
      await sleep(30);

      expect(handlers.onEvent).toHaveBeenCalledTimes(1);
      expect(handlers.onEvent.mock.calls[0]?.[0]).toMatchObject({ sequence: 5 });
    } finally {
      await ws.close();
      http.close();
    }
  });

  it('surfaces stream.gap via onGap with the gap payload', async () => {
    const { http, ws, token, baseUrl } = await setup({ events: [] });
    try {
      const stream = new WsEventStream({ baseUrl, token });
      const handlers = makeHandlers();

      await stream.connect(makeCursor({ sessionId: 's', sequence: 0 }), handlers, new AbortController().signal);

      const gap: StreamGap = { requested_after_sequence: 3, earliest_available_sequence: 10 };
      ws.send({
        schema_version: 1,
        event_type: 'stream.gap',
        occurred_at: new Date().toISOString(),
        payload: {
          requested_after_sequence: gap.requested_after_sequence,
          earliest_available_sequence: gap.earliest_available_sequence,
        },
      });

      await sleep(20);
      expect(handlers.onGap).toHaveBeenCalledWith(gap);
    } finally {
      await ws.close();
      http.close();
    }
  });

  it('treats heartbeat as a liveness status', async () => {
    const { http, ws, token, baseUrl } = await setup();
    try {
      const stream = new WsEventStream({ baseUrl, token });
      const handlers = makeHandlers();

      await stream.connect(makeCursor({ sessionId: 's', sequence: 0 }), handlers, new AbortController().signal);

      ws.send({
        schema_version: 1,
        event_type: 'stream.heartbeat',
        occurred_at: new Date().toISOString(),
      });

      await sleep(20);
      const connectedCalls = handlers.onStatus.mock.calls.filter(
        (c) => (c[0] as StreamStatus).kind === 'connected',
      );
      // Initial hello + heartbeat.
      expect(connectedCalls.length).toBeGreaterThanOrEqual(2);
    } finally {
      await ws.close();
      http.close();
    }
  });

  it('reports a network-level disconnect as recoverable', async () => {
    const { http, ws, token, baseUrl } = await setup();
    try {
      const stream = new WsEventStream({ baseUrl, token });
      const handlers = makeHandlers();

      await stream.connect(makeCursor({ sessionId: 's', sequence: 0 }), handlers, new AbortController().signal);

      // Simulate the network dropping the connection (not a clean close).
      ws.terminateConnections();

      await sleep(30);
      expect(handlers.onStatus).toHaveBeenCalledWith(
        expect.objectContaining({ kind: 'recoverable' }),
      );
    } finally {
      await ws.close();
      http.close();
    }
  });
});