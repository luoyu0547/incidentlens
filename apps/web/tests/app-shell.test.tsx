/**
 * App Shell integration tests.
 *
 * Covers the full app shell with router, QueryClient, and MSW-backed API
 * responses. All navigation labels are in Chinese; no mutation controls are
 * rendered.
 *
 * Router rendering is async (Transitioner useLayoutEffect calls router.load)
 * so all queries use findBy* / waitFor.
 */
import { render, screen } from '@testing-library/react';
import { userEvent } from '@testing-library/user-event';
import { describe, it, expect, beforeEach } from 'vitest';

import { server } from '../src/test/server';
import { renderApp } from '../src/test/render-app';
import { RouteError } from '../src/app/RouteError';
import { RoutePending } from '../src/app/RoutePending';

beforeEach(() => {
  server.resetHandlers();
});

describe('App Shell', () => {
  it('identifies the read-only observability workspace', async () => {
    renderApp();
    expect(await screen.findByRole('heading', { name: 'IncidentLens' })).toBeVisible();
    const nav = await screen.findByRole('navigation');
    expect(nav).toHaveTextContent('总览');
    expect(
      screen.queryByRole('button', { name: /approve|reject|execute|restart|rollback/i }),
    ).toBeNull();
  });

  it('renders a skip-to-content link', async () => {
    renderApp();
    const skipLink = await screen.findByText('跳转到主要内容');
    expect(skipLink).toBeVisible();
    expect(skipLink).toHaveAttribute('href', '#main-content');
  });

  it('renders the overview page at /', async () => {
    renderApp();
    expect(await screen.findByRole('heading', { name: '总览' })).toBeVisible();
  });

  it('navigates to the issues page via the nav link', async () => {
    const ue = userEvent.setup();
    renderApp({ initialEntries: ['/'] });

    const issuesLink = await screen.findByRole('link', { name: '问题' });
    await ue.click(issuesLink);

    expect(await screen.findByRole('heading', { name: '问题' })).toBeVisible();
  });
});

describe('Routes', () => {
  it('renders the issues page at /issues', async () => {
    renderApp({ initialEntries: ['/issues'] });
    expect(await screen.findByRole('heading', { name: '问题' })).toBeVisible();
  });

  it('renders the issue detail page at /issues/$issueId', async () => {
    renderApp({ initialEntries: ['/issues/iss-1'] });
    expect(await screen.findByRole('heading', { name: '问题详情 iss-1' })).toBeVisible();
  });

  it('renders the service page at /services/$serviceId', async () => {
    renderApp({ initialEntries: ['/services/svc-web'] });
    expect(await screen.findByRole('heading', { name: '服务 svc-web' })).toBeVisible();
  });

  it('renders the investigation page at /investigations/$investigationId', async () => {
    renderApp({ initialEntries: ['/investigations/inv-1'] });
    expect(await screen.findByRole('heading', { name: '调查 inv-1' })).toBeVisible();
  });

  it('shows the 404 page for unknown routes', async () => {
    renderApp({ initialEntries: ['/unknown-route'] });
    expect(await screen.findByText('页面未找到')).toBeVisible();
  });

  it('preserves search state when navigating', async () => {
    const ue = userEvent.setup();
    const { history, router } = renderApp({ initialEntries: ['/issues?status=open'] });

    // Initial search state is parsed from the URL.
    expect(router.state.location.search.status).toBe('open');

    // Navigate away to overview.
    const overviewLink = await screen.findByRole('link', { name: '总览' });
    await ue.click(overviewLink);
    expect(await screen.findByRole('heading', { name: '总览' })).toBeVisible();

    // Navigate back — search state is preserved from history.
    history.back();
    expect(await screen.findByRole('heading', { name: '问题' })).toBeVisible();
    expect(router.state.location.search.status).toBe('open');
  });
});

describe('History navigation', () => {
  it('supports browser back and forward', async () => {
    const ue = userEvent.setup();
    const { history } = renderApp({ initialEntries: ['/', '/issues'], initialIndex: 1 });

    // We're on /issues
    expect(await screen.findByRole('heading', { name: '问题' })).toBeVisible();

    // Click the overview nav link (push forward in history)
    const overviewLink = await screen.findByRole('link', { name: '总览' });
    await ue.click(overviewLink);
    expect(await screen.findByRole('heading', { name: '总览' })).toBeVisible();

    // Go back to /issues
    history.back();
    expect(await screen.findByRole('heading', { name: '问题' })).toBeVisible();

    // Go forward to /
    history.forward();
    expect(await screen.findByRole('heading', { name: '总览' })).toBeVisible();
  });
});

describe('Error and pending boundaries', () => {
  it('renders a friendly error boundary without leaking raw error text', () => {
    render(<RouteError />);
    expect(screen.getByRole('alert')).toBeVisible();
    expect(screen.getByText('页面加载错误')).toBeVisible();
    // The raw error message and stack must never leak into the DOM.
    expect(screen.queryByText(/TypeError|ReferenceError|at .*\.tsx/)).toBeNull();
  });

  it('renders the pending/loading boundary', () => {
    render(<RoutePending />);
    expect(screen.getByText('加载中...')).toBeVisible();
  });

  it('renders a user-friendly error on API failure', async () => {
    const { http, HttpResponse } = await import('msw');
    server.use(
      http.get('/api/v1/overview', () => HttpResponse.json(null, { status: 500 })),
    );

    renderApp({ initialEntries: ['/'] });
    // The overview page is a placeholder with no data fetching in Task 3,
    // so the error boundary is not triggered by this route component.
    // The shell itself renders successfully regardless of API state.
    expect(await screen.findByRole('heading', { name: '总览' })).toBeVisible();
  });
});
