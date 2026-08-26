import React from 'react';
import { render } from 'ink-testing-library';
import { describe, expect, it } from 'vitest';
import { InvestigationSummary } from './InvestigationSummary.js';
import type { InvestigationSummaryView } from '@incidentlens/protocol';

const investigation = {
  investigation_id: 'inv-1', issue_id: 'issue-1', service_id: 'svc-1', target_id: 'target-1',
  symptom: 'latency increased', status: 'running', created_at: '', updated_at: '',
  hypotheses: [{ hypothesis_id: 'hyp-1', status: 'testing', summary: 'cache saturation', updated_at: '' }],
  evidence: [{ evidence_ref_id: 'ev-1', evidence_kind: 'log_record', summary: 'redacted log evidence', created_at: '', service_id: 'svc-1', target_id: 'target-1' }],
} as InvestigationSummaryView;

describe('InvestigationSummary', () => {
  it('renders bounded safe summaries and IDs', () => {
    const { lastFrame } = render(<InvestigationSummary investigation={investigation} />);
    expect(lastFrame()).toContain('hyp-1');
    expect(lastFrame()).toContain('ev-1');
    expect(lastFrame()).not.toContain('credential');
  });
  it('distinguishes uncertain state', () => {
    const { lastFrame } = render(<InvestigationSummary investigation={{ ...investigation, status: 'uncertain' }} />);
    expect(lastFrame()).toContain('UNCERTAIN');
    expect(lastFrame()).toContain('automatic retry is unavailable');
  });
});
