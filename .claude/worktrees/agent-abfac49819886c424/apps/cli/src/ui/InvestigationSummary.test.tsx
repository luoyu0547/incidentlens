import React from 'react';
import { describe, expect, it } from 'vitest';
import { render } from 'ink-testing-library';
import { InvestigationSummary } from './InvestigationSummary.js';

describe('InvestigationSummary', () => {
  it('renders bounded safe evidence, todo, hypotheses, and child states', () => {
    const { lastFrame } = render(<InvestigationSummary noColor summary={{ investigation_id: 'inv-1', status: 'uncertain', conclusion: null, evidence: [{ evidence_ref_id: 'E-42', evidence_kind: 'log_record', created_at: '', service_id: 'svc', summary: 'safe evidence' }], hypotheses: [{ hypothesis_id: 'H-1', summary: 'safe hypothesis', status: 'open', updated_at: '' }], todos: [{ id: 'T-1', title: 'Check deployment', status: 'running' }], children: [{ id: 'C-1', title: 'Child check', status: 'uncertain' }] }} />);
    const frame = lastFrame();
    expect(frame).toContain('inv-1');
    expect(frame).toContain('E-42');
    expect(frame).toContain('H-1');
    expect(frame).toContain('T-1');
    expect(frame).toContain('C-1');
    expect(frame).toContain('uncertain');
    expect(frame).not.toContain('raw_output');
    expect(frame).not.toContain('hidden reasoning');
  });
});
