import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render } from 'ink-testing-library';
import { App } from '../../src/app/App.js';
import type { AppDependencies } from '../../src/app/dependencies.js';

function makeDeps(overrides: Partial<AppDependencies> = {}): AppDependencies {
  const target = { target_id: 'target-1', name: 'prod', host: 'prod.example', ssh_port: 22, ssh_user: 'ops' } as any;
  const session = { session_id: 'session-1', target_id: target.target_id, title: 'Incident' } as any;
  return {
    configStore: {
      load: vi.fn().mockResolvedValue({
        profileName: 'default', apiUrl: 'http://localhost:8000',
        lastTargetId: target.target_id, lastSessionId: session.session_id,
        lastSequenceBySession: { [session.session_id]: 7 },
      }),
      save: vi.fn().mockResolvedValue(undefined),
    } as any,
    tokenStore: { get: vi.fn().mockResolvedValue('token') } as any,
    api: {
      compatibility: vi.fn().mockResolvedValue({ protocol_version: '1.0.0' }),
      principal: vi.fn().mockResolvedValue({}),
      listTargets: vi.fn().mockResolvedValue([target]),
      getSession: vi.fn().mockResolvedValue(session),
      listMessages: vi.fn().mockResolvedValue([]),
      listApprovals: vi.fn().mockResolvedValue({ items: [] }),
      listEvents: vi.fn().mockResolvedValue({ items: [], has_more: false, next_after_sequence: 7 }),
    } as any,
    eventStream: { connect: vi.fn().mockResolvedValue(undefined) },
    now: () => new Date('2026-01-01T00:00:00Z'),
    exit: vi.fn(),
    ...overrides,
  };
}

describe('CLI flow integration', () => {
  it('bootstraps in order, restores the session, then opens the stream', async () => {
    const deps = makeDeps();
    const { lastFrame } = render(React.createElement(App, { dependencies: deps }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(lastFrame()).toContain('prod');
    expect(lastFrame()).toContain('Incident');
    expect(deps.api.compatibility).toHaveBeenCalled();
    expect(deps.api.getSession).toHaveBeenCalledWith('session-1');
    expect(deps.eventStream.connect).toHaveBeenCalledWith(
      { sessionId: 'session-1', sequence: 7 },
      expect.any(Object),
      expect.any(AbortSignal),
    );
  });

  it('does not cancel work when the stream disconnects', async () => {
    const connect = vi.fn(async (_cursor: unknown, handlers: any) => {
      handlers.onStatus({ kind: 'connected' });
      handlers.onStatus({ kind: 'recoverable', error: 'socket closed' });
    });
    const deps = makeDeps({ eventStream: { connect } as any });
    render(React.createElement(App, { dependencies: deps }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect((deps.api as any).cancelSession).not.toHaveBeenCalled();
  });
});
