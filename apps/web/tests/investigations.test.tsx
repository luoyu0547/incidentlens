import { describe, it, expect, beforeEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { screen } from '@testing-library/react';
import type { InvestigationSummaryView } from '@incidentlens/protocol';
import { server } from '../src/test/server';
import { renderApp } from '../src/test/render-app';

const investigation: InvestigationSummaryView = {
  investigation_id: 'inv-11', issue_id: 'iss-11', service_id: 'svc-web', target_id: 'host-a', status: 'waiting_approval', symptom: '延迟升高', created_at: '2026-08-24T01:00:00Z', started_at: '2026-08-24T01:01:00Z', updated_at: '2026-08-24T02:00:00Z',
  pending_approval_ids: ['approval-1'],
  milestones: [
    { event_id: 'later', event_type: 'investigation_completed', occurred_at: '2026-08-24T03:00:00Z', summary: '后一步' },
    { event_id: 'first', event_type: 'investigation_started', occurred_at: '2026-08-24T01:00:00Z', summary: '第一步' },
  ],
  hypotheses: [{ hypothesis_id: 'h-1', status: 'open', summary: '连接池耗尽', updated_at: '2026-08-24T02:00:00Z' }],
  evidence: [{ evidence_ref_id: 'ev-11', evidence_kind: 'log_record', summary: '脱敏日志', created_at: '2026-08-24T01:10:00Z', service_id: 'svc-web', target_id: 'host-a' }],
  conclusion: { summary: '等待批准后继续', evidence_ids: ['ev-11'] },
  change_summaries: [{ changeset_id: 'cs-1', status: 'draft', file_count: 1, scopes: ['config'] }],
};

beforeEach(() => server.resetHandlers());
function useInvestigation(data = investigation): void { server.use(http.get(/api\/v1\/investigations/, () => HttpResponse.json(investigation)), http.get(/api\/v1\/evidence/, () => HttpResponse.json({ content_redacted: 'server projection only', provenance: { evidence_ref_id: 'ev-11', incident_id: 'inv-11', service_id: 'svc-web', target_id: 'host-a', created_at: '2026-08-24T01:10:00Z', evidence_kind: 'log_record' } }))); }

describe('Investigation read', () => {
  it('renders status, summaries, hypotheses, conclusion and server-ordered milestones', async () => {
    useInvestigation(); renderApp({ initialEntries: ['/investigations/inv-11'] });
    expect(await screen.findByText('状态：waiting_approval')).toBeVisible();
    expect(screen.getByText('第一步')).toBeVisible(); expect(screen.getByText('后一步')).toBeVisible();
    const items = screen.getAllByRole('listitem'); expect(items.findIndex((item) => item.textContent?.includes('第一步'))).toBeLessThan(items.findIndex((item) => item.textContent?.includes('后一步')));
    expect(screen.getByText('连接池耗尽')).toBeVisible(); expect(screen.getByText('等待批准后继续')).toBeVisible();
  });
  it('states approval is handled in CLI without actionable controls or links', async () => {
    useInvestigation(); renderApp({ initialEntries: ['/investigations/inv-11'] });
    expect(await screen.findByText('需要在 CLI 中处理待审批事项。')).toBeVisible();
    expect(screen.queryByRole('button')).toBeNull(); expect(screen.queryByRole('link', { name: /CLI/i })).toBeNull();
  });
  it('lazy loads redacted evidence only when requested', async () => {
    useInvestigation(); renderApp({ initialEntries: ['/investigations/inv-11'] });
    expect(await screen.findByText('脱敏日志')).toBeVisible(); expect(screen.queryByText('server projection only')).toBeNull();
    screen.getByRole('button', { name: '查看已脱敏证据' }).click();
    expect(await screen.findByText('server projection only')).toBeVisible();
    expect(document.body.textContent).not.toMatch(/transcript|tool args|hidden reasoning|provider payload/i);
  });
});
