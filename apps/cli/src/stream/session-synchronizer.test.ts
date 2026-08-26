/**
 * Session synchronizer tests.
 *
 * Covers the recoverable synchronization contract from the task brief:
 * - committed-cursor persistence (lastSequenceBySession)
 * - duplicate/old event rejection
 * - unknown event cursor advancement
 * - heartbeat timeout treated as recoverable (fake timers)
 * - bounded 250 ms → 10 s exponential backoff (fake timers)
 * - 401 / version-incompatible fatal (no reconnect)
 * - gap snapshot sequence / authoritative replacement (no double-merge)
 * - disconnect without cancel
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import type { ControlPlaneApi } from '../api/control-plane-api.js';
import type { ConfigStore } from '../config/types.js';
import type { CliAction } from '../state/cli-state.js';
import type { KnownCliStreamEnvelope } from '@incidentlens/protocol';
import { SessionSynchronizer, type SessionSynchronizerOptions } from './session-synchronizer.js';
import type { EventStream, StreamCursor, StreamGap, StreamStatus } from './event-stream.js';
import { backoffDelay } from './reconnect-policy.js';

/* ------------------------------------------------------------------ */
/* Mock event stream                                                   */
/* ------------------------------------------------------------------ */

type StreamHandlers = Parameters<EventStream['connect']>[1];

class MockEventStream implements EventStream {
  cursor: StreamCursor = { sessionId: '', sequence: 0 };
  handlers: StreamHandlers | null = null;
  signal: AbortSignal | null = null;
  connectCount = 0;
  resolveConnect: (() => void) | null = null;

  connect(
    cursor: StreamCursor,
    handlers: StreamHandlers,
    signal: AbortSignal,
  ): Promise<void> {
    this.cursor = cursor;
    this.handlers = handlers;
    this.signal = signal;
    this.connectCount += 1;
    return new Promise((resolve) => {
      this.resolveConnect = resolve;
    });
  }

  /** Simulate the server hello completing the handshake. */
  hello(): void {
    this.handlers?.onStatus({ kind: 'connected' });
    this.resolveConnect?.();
  }

  emitEvent(sequence: number, event_type: string, payload: Record<string, unknown> = {}): void {
    const envelope = {
      schema_version: 1 as const,
      event_type,
      sequence,
      occurred_at: new Date().toISOString(),
      session_id: 'session-1',
      target_id: 'target-1',
      payload,
    } as KnownCliStreamEnvelope;
    this.handlers?.onEvent(envelope);
  }

  emitGap(gap: StreamGap): void {
    void this.handlers?.onGap(gap);
  }

  disconnect(): void {
    this.handlers?.onStatus({ kind: 'recoverable', error: 'connection lost' });
  }

  fatal(error: string, code?: string): void {
    this.handlers?.onStatus({ kind: 'fatal', error, code });
  }
}

/* ------------------------------------------------------------------ */
/* API + config fixtures                                               */
/* ------------------------------------------------------------------ */

function makeApi() {
  const api = {
    getSession: vi.fn(),
    listMessages: vi.fn(),
    listApprovals: vi.fn(),
    listEvents: vi.fn(),
    getOperation: vi.fn(),
    cancelSession: vi.fn(),
  } as unknown as ControlPlaneApi & Record<string, ReturnType<typeof vi.fn>>;
  return api;
}

function makeConfigStore(profile?: unknown) {
  const load = vi.fn().mockResolvedValue(
    profile ?? {
      profileName: 'default',
      apiUrl: 'https://api.example.com',
      lastSequenceBySession: {},
    },
  );
  const save = vi.fn().mockResolvedValue(undefined);
  const configStore = { load, save } as unknown as ConfigStore;
  return { configStore, load, save };
}

function makeHost() {
  const dispatch = vi.fn<(action: CliAction) => void>();
  const onCursorAdvance = vi.fn();
  return { dispatch, onCursorAdvance };
}

function makeOptions(
  stream: MockEventStream,
  api: ControlPlaneApi,
  configStore: ConfigStore,
  host: ReturnType<typeof makeHost>,
  overrides: Partial<SessionSynchronizerOptions> = {},
): SessionSynchronizerOptions {
  return {
    api,
    configStore,
    profileName: 'default',
    eventStream: stream,
    host,
    ...overrides,
  };
}

function makeSession(overrides: Record<string, unknown> = {}) {
  return {
    session_id: 'session-1',
    title: 'Prod incident',
    status: 'idle',
    target_id: 'target-1',
    service_id: null,
    investigation_id: null,
    owner: 'user@example.com',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

function makeOperation(overrides: Record<string, unknown> = {}) {
  return {
    operation_id: 'op-1',
    kind: 'agent_message',
    target_id: 'target-1',
    session_id: 'session-1',
    investigation_id: null,
    status: 'running',
    progress_summary: null,
    error_code: null,
    error_message: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    finished_at: null,
    ...overrides,
  };
}

function flush(times = 8): Promise<void> {
  let p = Promise.resolve();
  for (let i = 0; i < times; i += 1) {
    p = p.then(() => undefined);
  }
  return p;
}

/* ------------------------------------------------------------------ */
/* Tests                                                               */
/* ------------------------------------------------------------------ */

describe('sessionSynchronizer', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('connects at the committed cursor', async () => {
    const stream = new MockEventStream();
    const api = makeApi();
    const { configStore } = makeConfigStore();
    const host = makeHost();
    const sync = new SessionSynchronizer(makeOptions(stream, api, configStore, host));
    sync.setInitialCursor('session-1', 42);

    const controller = new AbortController();
    const started = sync.start(controller.signal);

    // The mock captured the cursor immediately.
    expect(stream.cursor).toEqual({ sessionId: 'session-1', sequence: 42 });

    stream.hello();
    await flush();
    controller.abort();
    await started;
  });

  it('persists the committed cursor to the profile on event advance', async () => {
    const stream = new MockEventStream();
    const api = makeApi();
    const { configStore, load, save } = makeConfigStore();
    const host = makeHost();
    const sync = new SessionSynchronizer(makeOptions(stream, api, configStore, host));
    sync.setInitialCursor('session-1', 0);

    const controller = new AbortController();
    const started = sync.start(controller.signal);
    stream.hello();
    await flush();

    stream.emitEvent(7, 'tool.proposed', { tool_id: 't1' });
    await flush();

    expect(host.dispatch).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'stream_event' }),
    );
    expect(host.onCursorAdvance).toHaveBeenCalledWith('session-1', 7);
    expect(save).toHaveBeenCalledWith(
      expect.objectContaining({
        lastSequenceBySession: { 'session-1': 7 },
      }),
    );

    controller.abort();
    await started;
  });

  it('rejects duplicate and old sequence events', async () => {
    const stream = new MockEventStream();
    const api = makeApi();
    const { configStore } = makeConfigStore();
    const host = makeHost();
    const sync = new SessionSynchronizer(makeOptions(stream, api, configStore, host));
    sync.setInitialCursor('session-1', 5);

    const controller = new AbortController();
    const started = sync.start(controller.signal);
    stream.hello();
    await flush();

    stream.emitEvent(4, 'tool.proposed', { tool_id: 't1' });
    stream.emitEvent(5, 'tool.proposed', { tool_id: 't2' });
    await flush();

    // Neither should dispatch: 4 is old, 5 is the current cursor.
    expect(host.dispatch).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: 'stream_event' }),
    );

    controller.abort();
    await started;
  });

  it('advances the cursor for unknown event types without UI mutation', async () => {
    // The transport already filters unknown event types (see
    // ws-event-stream tests). For events that still reach onEvent, the
    // synchronizer advances the cursor so a subsequent reconnect resumes
    // after them — the reducer is what ignores unrecognized payloads.
    const stream = new MockEventStream();
    const api = makeApi();
    const { configStore, save } = makeConfigStore();
    const host = makeHost();
    const sync = new SessionSynchronizer(makeOptions(stream, api, configStore, host));
    sync.setInitialCursor('session-1', 0);

    const controller = new AbortController();
    const started = sync.start(controller.signal);
    stream.hello();
    await flush();

    stream.emitEvent(9, 'future.event.type', { x: 1 });
    await flush();

    // The event is dispatched to the reducer (which ignores unknown
    // payloads), and the cursor advances + persists.
    expect(host.dispatch).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'stream_event' }),
    );
    expect(host.onCursorAdvance).toHaveBeenCalledWith('session-1', 9);
    expect(save).toHaveBeenCalledWith(
      expect.objectContaining({ lastSequenceBySession: { 'session-1': 9 } }),
    );

    controller.abort();
    await started;
  });

  it('treats heartbeat timeout as recoverable and reconnects', async () => {
    const stream = new MockEventStream();
    const api = makeApi();
    const { configStore } = makeConfigStore();
    const host = makeHost();
    const sync = new SessionSynchronizer(
      makeOptions(stream, api, configStore, host, { heartbeatTimeoutMs: 50 }),
    );
    sync.setInitialCursor('session-1', 0);

    const controller = new AbortController();
    const started = sync.start(controller.signal);
    stream.hello();
    await flush();

    expect(stream.connectCount).toBe(1);

    // No heartbeat/event for 50 ms → heartbeat timeout.
    await vi.advanceTimersByTimeAsync(50);
    await flush();

    // Reconnect after backoff (250 ms).
    await vi.advanceTimersByTimeAsync(250);
    await flush();

    expect(host.dispatch).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'set_stream_status',
        status: expect.objectContaining({ error: 'Heartbeat timeout' }),
      }),
    );
    expect(stream.connectCount).toBe(2);

    controller.abort();
    await started;
  });

  it('does not time out when a heartbeat resets the timer', async () => {
    const stream = new MockEventStream();
    const api = makeApi();
    const { configStore } = makeConfigStore();
    const host = makeHost();
    const sync = new SessionSynchronizer(
      makeOptions(stream, api, configStore, host, { heartbeatTimeoutMs: 100 }),
    );
    sync.setInitialCursor('session-1', 0);

    const controller = new AbortController();
    const started = sync.start(controller.signal);
    stream.hello();
    await flush();

    // Heartbeat every 50 ms keeps the connection alive past the 100 ms mark.
    for (let i = 0; i < 4; i += 1) {
      await vi.advanceTimersByTimeAsync(50);
      stream.handlers?.onStatus({ kind: 'connected' });
    }

    expect(stream.connectCount).toBe(1);
    expect(host.dispatch).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: 'set_stream_status', status: expect.objectContaining({ error: 'Heartbeat timeout' }) }),
    );

    controller.abort();
    await started;
  });

  it('does not send cancel on disconnect (only explicit /cancel does)', async () => {
    const stream = new MockEventStream();
    const api = makeApi();
    const { configStore } = makeConfigStore();
    const host = makeHost();
    const sync = new SessionSynchronizer(makeOptions(stream, api, configStore, host));
    sync.setInitialCursor('session-1', 0);

    const controller = new AbortController();
    const started = sync.start(controller.signal);
    stream.hello();
    await flush();

    // Network disconnect — recoverable.
    stream.disconnect();
    await flush();
    await vi.advanceTimersByTimeAsync(250);
    await flush();

    // Only reconnect; no cancel API call.
    expect(stream.connectCount).toBe(2);
    expect(api.cancelSession).not.toHaveBeenCalled();

    controller.abort();
    await started;
  });

  it('marks 401/version-incompatible as fatal and does not reconnect', async () => {
    const stream = new MockEventStream();
    const api = makeApi();
    const { configStore } = makeConfigStore();
    const host = makeHost();
    const sync = new SessionSynchronizer(makeOptions(stream, api, configStore, host));
    sync.setInitialCursor('session-1', 0);

    const controller = new AbortController();
    const started = sync.start(controller.signal);
    stream.hello();
    await flush();

    stream.fatal('authentication required', '4401');
    await flush();
    await vi.advanceTimersByTimeAsync(10_000);
    await flush();

    expect(host.dispatch).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'set_stream_status',
        status: expect.objectContaining({ connected: false, error: 'authentication required' }),
      }),
    );
    // No reconnect after fatal.
    expect(stream.connectCount).toBe(1);

    controller.abort();
    await started;
  });

  it('recovers from a gap by replacing the projection from an authoritative snapshot', async () => {
    const stream = new MockEventStream();
    const api = makeApi();
    (api.getSession as ReturnType<typeof vi.fn>).mockResolvedValue(makeSession());
    (api.listMessages as ReturnType<typeof vi.fn>).mockResolvedValue([
      { message_id: 'm1', content: 'Recovered message', role: 'assistant', session_id: 'session-1', created_at: '2026-01-01T00:00:00Z' },
    ]);
    (api.listApprovals as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [
        {
          approval_id: 'a1',
          status: 'pending',
          decision_status: 'pending',
          downstream_status: 'pending',
          intent_summary: 'Apply change',
          risk: 'low',
          kind: 'change',
          expires_at: '2026-02-01T00:00:00Z',
          created_at: '2026-01-01T00:00:00Z',
          linkage: { session_id: 'session-1' },
        },
      ],
      has_more: false,
    });
    (api.listEvents as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [
        { sequence: 100, event_type: 'operation.running', event_id: 'x', payload: { operation_id: 'op-1' } },
        { sequence: 101, event_type: 'operation.succeeded', event_id: 'y', payload: { operation_id: 'op-1' } },
      ],
      latest_sequence: 101,
      earliest_available_sequence: 90,
      next_after_sequence: 101,
      has_more: false,
    });
    (api.getOperation as ReturnType<typeof vi.fn>).mockResolvedValue(makeOperation());

    const { configStore, save } = makeConfigStore();
    const host = makeHost();
    const sync = new SessionSynchronizer(makeOptions(stream, api, configStore, host));
    sync.setInitialCursor('session-1', 88);

    const controller = new AbortController();
    const started = sync.start(controller.signal);
    stream.hello();
    await flush();

    stream.emitGap({ requested_after_sequence: 88, earliest_available_sequence: 90 });
    await vi.advanceTimersByTimeAsync(0);
    await flush();

    // gap_snapshot dispatched with the authoritative replacement.
    const gapAction = host.dispatch.mock.calls.find(
      (c) => (c[0] as CliAction).type === 'gap_snapshot',
    );
    expect(gapAction).toBeDefined();
    const snapshot = (gapAction?.[0] as { type: 'gap_snapshot'; snapshot: { sequence: number } }).snapshot;
    expect(snapshot.sequence).toBe(101);
    expect(host.dispatch).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'gap_snapshot' }),
    );

    // Cursor persisted to the authoritative sequence.
    expect(save).toHaveBeenCalledWith(
      expect.objectContaining({ lastSequenceBySession: { 'session-1': 101 } }),
    );

    controller.abort();
    await started;
  });
});

/* ------------------------------------------------------------------ */
/* Backoff policy                                                      */
/* ------------------------------------------------------------------ */

describe('backoffDelay', () => {
  it('starts at 250 ms and doubles with a 10 s ceiling', () => {
    expect(backoffDelay(0)).toBe(250);
    expect(backoffDelay(1)).toBe(500);
    expect(backoffDelay(2)).toBe(1000);
    expect(backoffDelay(3)).toBe(2000);
    expect(backoffDelay(4)).toBe(4000);
    expect(backoffDelay(5)).toBe(8000);
    expect(backoffDelay(6)).toBe(10000);
    expect(backoffDelay(10)).toBe(10000);
  });
});