import React from 'react';
import { render } from 'ink-testing-library';
import { describe, expect, it, afterEach } from 'vitest';
import { ToolCard } from './ToolCard.js';

const base = { kind: 'tool' as const, toolId: 'tool-1', toolName: 'query_logs', status: 'running' as const };
afterEach(() => { delete process.env.NO_COLOR; });

describe('ToolCard', () => {
  it.each(['proposed', 'running', 'succeeded', 'failed', 'uncertain'] as const)('renders %s safely', (status) => {
    const { lastFrame } = render(<ToolCard tool={{ ...base, status, summary: 'safe summary', error: status === 'failed' || status === 'uncertain' ? 'safe error' : undefined }} />);
    expect(lastFrame()).toContain(status === 'uncertain' ? 'UNCERTAIN' : status.toUpperCase());
    expect(lastFrame()).not.toContain('password');
  });
  it('honors NO_COLOR', () => {
    process.env.NO_COLOR = '1';
    const { lastFrame } = render(<ToolCard tool={base} />);
    expect(lastFrame()).not.toContain('[');
  });
});
