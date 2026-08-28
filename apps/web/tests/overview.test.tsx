/**
 * Cloud overview page tests.
 *
 * Covers host/target safety labels, service health, active issue counts,
 * resolution/verification summaries, navigation links, and the distinct empty /
 * degraded states (no target, no discovered services, no issues, unknown health,
 * API unavailable). All navigation labels are in Chinese; no mutation controls
 * are rendered.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { screen, within } from '@testing-library/react';
import type { OverviewView } from '@incidentlens/protocol';

import { server } from '../src/test/server';
import { renderApp } from '../src/test/render-app';

const GENERATED_AT = '2026-08-26T09:30:00Z';

/** A rich, coherent overview used by the happy-path tests. */
const OVERVIEW: OverviewView = {
  generated_at: GENERATED_AT,
  open_issue_count: 1,
  active_investigation_count: 1,
  pending_approval_count: 0,
  service_counts: { healthy: 1, degraded: 1, unreachable: 0, unknown: 0 },
  targets: [
    {
      target_id: 'tgt-host-a',
      name: 'host-a',
      host: 'host-a.internal',
      status: 'degraded',
      service_count: 2,
      last_tested_at: '2026-08-26T09:20:00Z',
      last_observed_at: '2026-08-26T09:25:00Z',
      services: [
        {
          service_id: 'svc-web',
          status: 'degraded',
          container_count: 2,
          open_issue_count: 1,
          pending_approval_count: 0,
          last_observed_at: '2026-08-26T09:25:00Z',
        },
        {
          service_id: 'svc-db',
          status: 'healthy',
          container_count: 1,
          open_issue_count: 0,
          pending_approval_count: 0,
          last_observed_at: '2026-08-26T09:22:00Z',
        },
      ],
    },
  ],
  recent_resolutions: [
    {
      investigation_id: 'inv-1',
      issue_id: 'iss-1',
      target_id: 'tgt-host-a',
      service_id: 'svc-web',
      symptom: 'web 服务响应时间升高',
      resolution_summary: '已扩容并重启 web 服务',
      verification_summary: '错误率已回落至基线',
      resolved_at: '2026-08-26T09:00:00Z',
    },
  ],
};

const NO_TARGETS: OverviewView = {
  generated_at: GENERATED_AT,
  open_issue_count: 0,
  active_investigation_count: 0,
  pending_approval_count: 0,
  service_counts: {},
  targets: [],
  recent_resolutions: [],
};

const NO_SERVICES: OverviewView = {
  generated_at: GENERATED_AT,
  open_issue_count: 0,
  active_investigation_count: 0,
  pending_approval_count: 0,
  service_counts: {},
  targets: [
    {
      target_id: 'tgt-host-a',
      name: 'host-a',
      host: 'host-a.internal',
      status: 'degraded',
      service_count: 0,
      services: [],
    },
  ],
  recent_resolutions: [],
};

const NO_ISSUES: OverviewView = {
  generated_at: GENERATED_AT,
  open_issue_count: 0,
  active_investigation_count: 0,
  pending_approval_count: 0,
  service_counts: { healthy: 1 },
  targets: [
    {
      target_id: 'tgt-host-a',
      name: 'host-a',
      host: 'host-a.internal',
      status: 'healthy',
      service_count: 1,
      services: [
        {
          service_id: 'svc-web',
          status: 'healthy',
          container_count: 2,
          open_issue_count: 0,
          pending_approval_count: 0,
        },
      ],
    },
  ],
  recent_resolutions: [],
};

const UNKNOWN_HEALTH: OverviewView = {
  generated_at: GENERATED_AT,
  open_issue_count: 0,
  active_investigation_count: 0,
  pending_approval_count: 0,
  service_counts: { unknown: 1 },
  targets: [
    {
      target_id: 'tgt-host-a',
      name: 'host-a',
      host: 'host-a.internal',
      status: 'unknown',
      service_count: 1,
      services: [
        {
          service_id: 'svc-web',
          status: 'unknown',
          container_count: 0,
          open_issue_count: 0,
          pending_approval_count: 0,
          last_observed_at: null,
        },
      ],
    },
  ],
  recent_resolutions: [],
};

function useOverview(data: OverviewView | null, status = 200) {
  server.use(http.get('/api/v1/overview', () => HttpResponse.json(data, { status })));
}

beforeEach(() => {
  server.resetHandlers();
});

describe('Overview page', () => {
  it('renders target and host safety labels', async () => {
    useOverview(OVERVIEW);
    renderApp({ initialEntries: ['/'] });

    const targetSection = await screen.findByRole('region', { name: '目标状态' });
    expect(within(targetSection).getByText('host-a')).toBeVisible();
    expect(within(targetSection).getByText(/host-a\.internal/)).toBeVisible();
  });

  it('renders service health, container, and open-issue counts', async () => {
    useOverview(OVERVIEW);
    renderApp({ initialEntries: ['/'] });

    const table = await screen.findByRole('table');
    const webRow = within(table).getByText('svc-web').closest('tr');
    expect(webRow).not.toBeNull();
    expect(within(webRow!).getByText('降级')).toBeVisible();
    expect(within(webRow!).getByText('2')).toBeVisible();
    expect(within(webRow!).getByText('1')).toBeVisible();

    const dbRow = within(table).getByText('svc-db').closest('tr');
    expect(dbRow).not.toBeNull();
    expect(within(dbRow!).getByText('健康')).toBeVisible();
    expect(within(dbRow!).getByText('1')).toBeVisible();
  });

  it('summarizes active issues and links to the issue list', async () => {
    useOverview(OVERVIEW);
    renderApp({ initialEntries: ['/'] });

    const issuesSection = await screen.findByRole('region', { name: '活动问题' });
    const issueLink = within(issuesSection).getByRole('link', { name: '开放问题：1' });
    expect(issueLink).toHaveAttribute('href', '/issues');
    expect(within(issuesSection).getByText('svc-web')).toBeVisible();
  });

  it('shows recent resolution and verification summaries', async () => {
    useOverview(OVERVIEW);
    renderApp({ initialEntries: ['/'] });

    const resultsSection = await screen.findByRole('region', { name: '最近处理结果' });
    expect(within(resultsSection).getByText('web 服务响应时间升高')).toBeVisible();
    expect(within(resultsSection).getByText('已扩容并重启 web 服务')).toBeVisible();
    expect(within(resultsSection).getByText('验证：错误率已回落至基线')).toBeVisible();
  });

  it('produces navigation links to service, issue, and investigation reads', async () => {
    useOverview(OVERVIEW);
    renderApp({ initialEntries: ['/'] });

    const table = await screen.findByRole('table');
    expect(within(table).getByRole('link', { name: 'svc-web' })).toHaveAttribute(
      'href',
      '/services/svc-web?levels=%5B%5D&mode=history&context=20&follow=true',
    );

    const issuesSection = screen.getByRole('region', { name: '活动问题' });
    expect(within(issuesSection).getByRole('link', { name: '开放问题：1' })).toHaveAttribute(
      'href',
      '/issues',
    );

    const resultsSection = screen.getByRole('region', { name: '最近处理结果' });
    expect(within(resultsSection).getByRole('link', { name: '查看问题' })).toHaveAttribute(
      'href',
      '/issues/iss-1',
    );
    expect(within(resultsSection).getByRole('link', { name: '查看调查' })).toHaveAttribute(
      'href',
      '/investigations/inv-1',
    );
  });

  it('exposes no mutation controls and no credential or raw-path material', async () => {
    useOverview(OVERVIEW);
    renderApp({ initialEntries: ['/'] });

    await screen.findByRole('region', { name: '目标状态' });
    expect(
      screen.queryByRole('button', { name: /approve|reject|execute|restart|rollback/i }),
    ).toBeNull();

    const bodyText = document.body.textContent ?? '';
    for (const forbidden of [
      'authentication_hint',
      'host_key_policy',
      'ssh_user',
      'pinned_host_key',
      'allowed_host_paths',
      'optional_source_path',
    ]) {
      expect(bodyText).not.toContain(forbidden);
    }
  });

  it('renders the overview generated-at timestamp', async () => {
    useOverview(OVERVIEW);
    renderApp({ initialEntries: ['/'] });

    await screen.findByRole('region', { name: '目标状态' });
    expect(document.querySelector(`time[datetime="${GENERATED_AT}"]`)).not.toBeNull();
  });

  it('renders the empty state when no targets are discovered', async () => {
    useOverview(NO_TARGETS);
    renderApp({ initialEntries: ['/'] });

    expect(await screen.findByText('未发现目标')).toBeVisible();
    expect(screen.getByText('未发现服务')).toBeVisible();
    expect(screen.getByText('无活动问题')).toBeVisible();
    expect(screen.getByText('暂无处理结果')).toBeVisible();
  });

  it('shows the empty service state while a target remains', async () => {
    useOverview(NO_SERVICES);
    renderApp({ initialEntries: ['/'] });

    expect(await screen.findByText('host-a')).toBeVisible();
    expect(screen.getByText('未发现服务')).toBeVisible();
  });

  it('shows no-active-issues and no-resolution messages when there are none', async () => {
    useOverview(NO_ISSUES);
    renderApp({ initialEntries: ['/'] });

    await screen.findByRole('region', { name: '活动问题' });
    expect(screen.getByText('无活动问题')).toBeVisible();
    expect(screen.getByText('暂无处理结果')).toBeVisible();
  });

  it('renders an explicit badge for unknown health', async () => {
    useOverview(UNKNOWN_HEALTH);
    renderApp({ initialEntries: ['/'] });

    await screen.findByRole('region', { name: '目标状态' });
    // The target and its service are both unknown → at least two explicit badges.
    expect(screen.getAllByText('未知').length).toBeGreaterThanOrEqual(2);
  });

  it('renders a friendly error when the API is unavailable', async () => {
    useOverview(null, 500);
    renderApp({ initialEntries: ['/'] });

    expect(await screen.findByRole('heading', { name: '总览' })).toBeVisible();
    expect(await screen.findByText('加载页面时出现问题，请稍后重试。')).toBeVisible();
  });
});
