import React from 'react';
import { render } from 'ink-testing-library';
import { describe, expect, it } from 'vitest';
import type { InvestigationSummaryView } from '@incidentlens/protocol';
import { InvestigationSummary } from './InvestigationSummary.js';

const summary: InvestigationSummaryView = {
  investigation_id: 'inv-1',
  issue_id: 'issue-1',
  service_id: 'svc-1',
  target_id: 'target-1',
  symptom: 'latency increased',
  status: 'running',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  hypotheses: [{
    hypothesis_id: 'hyp-1',
    status: 'testing',
    summary: 'cache saturation',
    updated_at: '2026-01-01T00:00:00Z',
  }],
  evidence: [{
    evidence_ref_id: 'ev-1',
    evidence_kind: 'log_record',
    summary: 'redacted log evidence',
    created_at: '2026-01-01T00:00:00Z',
    service_id: 'svc-1',
    target_id: 'target-1',
  }],
};

describe('InvestigationSummary', () => {
  it('renders safe IDs and bounded summaries without sensitive fields', () => {
    const { lastFrame } = render(<InvestigationSummary investigation={summary} />);
    const frame = lastFrame();
    expect(frame).toContain('hyp-1');
    expect(frame).toContain('ev-1');
    expect(frame).toContain('cache saturation');
    expect(frame).not.toContain('credential');
    expect(frame).not.toContain('raw output');
  });

  it('marks uncertain investigations and does not offer retry', () => {
    const { lastFrame } = render(<InvestigationSummary investigation={{ ...summary, status: 'uncertain' }} />);
    const frame = lastFrame();
    expect(frame).toContain('UNCERTAIN');
    expect(frame).toContain('automatic retry is unavailable');
    expect(frame).not.toContain('Retry:');
  });

  it('honors NO_COLOR', () => {
    const previous = process.env.NO_COLOR;
    process.env.NO_COLOR = '1';
    try {
      const { lastFrame } = render(<InvestigationSummary investigation={summary} />);
      expect(lastFrame()).not.toContain('[');
    } finally {
      if (previous === undefined) delete process.env.NO_COLOR;
      else process.env.NO_COLOR = previous;
    }
  });
});
