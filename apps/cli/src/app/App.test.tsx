import { describe, expect, it, vi } from 'vitest';
import { render } from 'ink-testing-library';
import React from 'react';
import { Text } from 'ink';
import { App } from './App.js';
import type { ApprovalDetailView } from '@incidentlens/protocol';
import type { AppDependencies } from './dependencies.js';
import type { ApprovalBlock } from '../state/cli-state.js';

const settle = async (): Promise<void> => {
  await new Promise((resolve) => setTimeout(resolve, 20));
};

describe('Ink rendering', () => {
  it('renders simple text', () => {
    const { lastFrame } = render(<Text>Hello World</Text>);

    expect(lastFrame()).toContain('Hello World');
  });

  it('renders colored text', () => {
    const { lastFrame } = render(<Text color="blue">Blue Text</Text>);

    expect(lastFrame()).toContain('Blue Text');
  });

  it('renders server-authoritative pending approval actions through App', () => {
    const approval: ApprovalBlock = { kind: 'approval', approvalId: 'apr-1', status: 'pending' };
    const approvalView = {
      approval_id: 'apr-1',
      decision_status: 'pending',
      status: 'approved',
      downstream_status: 'pending',
      intent_summary: 'Restart the degraded service',
      risk: 'high',
      kind: 'restart',
      expires_at: '2026-01-01T01:00:00Z',
      created_at: '2026-01-01T00:00:00Z',
      linkage: {},
    } as ApprovalDetailView;
    const deps = {
      api: {
        compatibility: vi.fn().mockResolvedValue({ protocol_version: '1.0.0' }),
        principal: vi.fn().mockResolvedValue({}),
      },
      configStore: { load: vi.fn().mockResolvedValue(null), save: vi.fn() },
      tokenStore: { get: vi.fn().mockResolvedValue('token') },
      eventStream: { connect: vi.fn().mockResolvedValue(undefined) },
      now: () => new Date('2026-01-01T00:00:00Z'),
      exit: vi.fn(),
    } as unknown as AppDependencies;
    const { lastFrame } = render(
      <App
        dependencies={deps}
        initialState={{ bootstrap: 'ready', messages: [approval], approvals: { 'apr-1': approvalView } }}
      />,
    );

    const frame = lastFrame() ?? '';
    expect(frame).toContain('需要审批 · restart · apr-1');
    expect(frame).toMatch(/yes — 允许本次.*no — 拒绝本次.*yes all — 允许本 session 后续审批/s);
    expect(frame).toContain('1 pending approval(s)');
    expect(frame).not.toContain('approved');
  });

  it('routes wizard input exclusively to the target overlay', async () => {
    const deps = {
      api: {},
      configStore: { load: vi.fn().mockResolvedValue(null), save: vi.fn() },
      tokenStore: { get: vi.fn().mockResolvedValue('token') },
      eventStream: { connect: vi.fn().mockResolvedValue(undefined) },
      now: () => new Date('2026-01-01T00:00:00Z'),
      exit: vi.fn(),
    } as unknown as AppDependencies;
    const instance = render(
      <App
        dependencies={deps}
        initialState={{
          bootstrap: 'ready',
          overlay: { kind: 'target-wizard', mode: 'create', step: 'name' },
        }}
      />,
    );
    await settle();

    instance.stdin.write('incidentlens-tencent');
    await settle();
    instance.stdin.write('\r');
    await settle();

    const frame = instance.lastFrame() ?? '';
    expect(frame).toContain('Host');
    expect(frame).not.toContain('No target selected');
    expect(frame).not.toContain('Type a message or / for commands');
  });
});
