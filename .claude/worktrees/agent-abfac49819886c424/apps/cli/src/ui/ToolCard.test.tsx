import React from 'react';
import { describe, expect, it } from 'vitest';
import { render } from 'ink-testing-library';
import { ToolCard } from './ToolCard.js';
import { ProgressItem } from './ProgressItem.js';

describe('ToolCard', () => {
  it.each(['proposed', 'running', 'succeeded', 'failed', 'uncertain'] as const)('renders %s safely', (status) => {
    const { lastFrame } = render(<ToolCard tool={{ toolId: 't1', toolName: 'inspect', status, summary: 'safe summary', error: status === 'failed' ? 'safe error' : undefined }} />);
    expect(lastFrame()).toContain(status);
    expect(lastFrame()).not.toContain('raw args');
    expect(lastFrame()).not.toContain('TOP_SECRET');
  });

  it('bounds summaries, preserves uncertain distinction, and supports NO_COLOR', () => {
    const { lastFrame } = render(<ToolCard noColor tool={{ toolId: 't1', toolName: 'inspect', status: 'uncertain', summary: 'x'.repeat(100) }} maxSummaryLength={20} />);
    expect(lastFrame()).toContain('uncertain');
    expect(lastFrame()).toContain('…');
    expect(lastFrame()).not.toContain('[');
  });

  it('renders safe progress item kinds without raw fields', () => {
    const { lastFrame } = render(<ProgressItem item={{ kind: 'evidence', id: 'E-1', summary: 'bounded evidence' }} noColor />);
    expect(lastFrame()).toContain('E-1');
    expect(lastFrame()).not.toContain('provider');
    expect(lastFrame()).not.toContain('credential');
    expect(lastFrame()).not.toContain('reasoning');
  });
});
