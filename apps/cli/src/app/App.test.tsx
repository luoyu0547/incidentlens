import { describe, expect, it, vi } from 'vitest';
import { render } from 'ink-testing-library';
import React from 'react';
import { Text } from 'ink';
import { App } from './App.js';
import type { AppDependencies } from './dependencies.js';
import type { ApprovalBlock } from '../state/cli-state.js';

describe('Ink rendering', () => {
  it('renders simple text', () => {
    const { lastFrame } = render(<Text>Hello World</Text>);

    expect(lastFrame()).toContain('Hello World');
  });

  it('renders colored text', () => {
    const { lastFrame } = render(<Text color="blue">Blue Text</Text>);

    expect(lastFrame()).toContain('Blue Text');
  });

  it('renders App approval state from an event without marking it persisted', async () => {
    const approval: ApprovalBlock = { kind: 'approval', approvalId: 'apr-1', status: 'pending' };
    const approve = vi.fn();
    const reject = vi.fn();
    const deps = {
      api: {
        compatibility: vi.fn().mockResolvedValue({ protocol_version: '1.0.0' }),
        principal: vi.fn().mockResolvedValue({}),
        approve,
        reject,
      },
      configStore: { load: vi.fn().mockResolvedValue(null), save: vi.fn() },
      tokenStore: { get: vi.fn().mockResolvedValue('token') },
      eventStream: { connect: vi.fn().mockResolvedValue(undefined) },
      now: () => new Date('2026-01-01T00:00:00Z'),
      exit: vi.fn(),
    } as unknown as AppDependencies;
    const { lastFrame } = render(<App dependencies={deps} initialState={{ bootstrap: 'ready', messages: [approval] }} />);

    expect(lastFrame()).toContain('Approval: apr-1');
    expect(lastFrame()).not.toContain('approved');
    expect(lastFrame()).not.toContain('已批准');
  });
});
