import { describe, it, expect, beforeEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { screen } from '@testing-library/react';
import type { IssuePage, IssueView } from '@incidentlens/protocol';
import { server } from '../src/test/server';
import { renderApp } from '../src/test/render-app';

const base: IssueView = {
  issue_id: 'iss-11', service_id: 'svc-web', target_id: 'host-a', investigation_id: 'inv-11',
  status: 'resolved', severity: 'error', symptom: '延迟升高', created_at: '2026-08-24T01:02:03Z',
  updated_at: '2026-08-24T04:05:06Z', started_at: '2026-08-24T01:03:00Z', completed_at: '2026-08-24T04:00:00Z',
  root_cause: '连接池耗尽', root_cause_confidence: 0,
  evidence: [{ evidence_ref_id: 'ev-11', evidence_kind: 'log_record', summary: '连接池 exhausted', created_at: '2026-08-24T01:04:00Z', service_id: 'svc-web', target_id: 'host-a' }],
  resolution: { changeset_id: 'cs-11', status: 'validated', file_count: 1, scopes: ['config'] },
  verification: { evidence_ref_id: 'ev-v', passed: true, summary: '延迟恢复', created_at: '2026-08-24T04:01:00Z' },
};

beforeEach(() => server.resetHandlers());
function issueResponse(issue: IssueView = base): void { server.use(http.get(/api\/v1\/issues/, ({ request }) => { const url = new URL(request.url); expect(url.searchParams.get('status')).toBe('resolved'); expect(url.searchParams.get('service_id')).toBe('svc-web'); return HttpResponse.json({ has_more: false, next_cursor: null, items: [issue] } satisfies IssuePage); }), http.get(/api\/v1\/issues/, () => HttpResponse.json(issue))); }

describe('Issues reads', () => {
  it('forwards URL filters and renders projected list fields', async () => {
    issueResponse(); renderApp({ initialEntries: ['/issues?status=resolved&service_id=svc-web'] });
    expect(await screen.findByText('延迟升高')).toBeVisible();
    expect(screen.getByText(/连接池耗尽/)).toBeVisible();
    expect(document.querySelector('time[datetime="2026-08-24T01:02:03Z"]')).not.toBeNull();
  });
  it('renders zero confidence rather than treating it as missing', async () => {
    issueResponse(); renderApp({ initialEntries: ['/issues/iss-11'] });
    expect(await screen.findByText(/根因置信度：0/)).toBeVisible();
  });
  it('keeps null root cause and confidence explicit', async () => {
    issueResponse({ ...base, root_cause: null, root_cause_confidence: null }); renderApp({ initialEntries: ['/issues/iss-11'] });
    expect(await screen.findByText('根因：未知')).toBeVisible(); expect(screen.getByText('根因置信度：未提供')).toBeVisible();
  });
  it('shows resolution, verification, and no mutation controls', async () => {
    issueResponse(); renderApp({ initialEntries: ['/issues/iss-11'] });
    expect(await screen.findByRole('region', { name: '验证结果' })).toHaveTextContent('通过');
    expect(screen.getByRole('region', { name: '处理结果' })).toHaveTextContent('validated');
    expect(screen.queryByRole('button', { name: /approve|reject|execute|rollback|restart/i })).toBeNull();
    expect(document.body.textContent).not.toMatch(/transcript|tool args|hidden reasoning|provider payload/i);
  });
});
