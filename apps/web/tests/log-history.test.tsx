import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { server } from '../src/test/server';
import { LogViewer } from '../src/logs/LogViewer';
import { normalizeLogRouteSearch } from '../src/logs/log-search';

const page = (id: string, cursor: string | null, hasMore: boolean) => ({ has_more: hasMore, next_cursor: cursor, previous_cursor: null, snapshot_cursor: 'snap', items: [{ log_id: id, cursor: id, message: id, occurred_at: '2026-01-01T00:00:00Z', severity: 'info' as const }] });

describe('log history', () => {
  it('loads one page and preserves backend order', async () => {
    server.use(http.get('/api/v1/services/svc/logs', ({ request }) => HttpResponse.json(page(new URL(request.url).searchParams.get('before') ? 'second' : 'first', 'opaque', true))));
    render(<QueryClientProvider client={new QueryClient()}><LogViewer serviceId="svc" targetId="tgt" initialSearch={normalizeLogRouteSearch({})} /></QueryClientProvider>);
    expect(await screen.findByText('first')).toBeVisible();
    expect(screen.getByRole('button', { name: '加载更早日志' })).toBeVisible();
  });
  it('does not request reversed ranges', () => {
    const request = vi.fn();
    server.use(http.get('/api/v1/services/svc/logs', request));
    render(<QueryClientProvider client={new QueryClient()}><LogViewer serviceId="svc" targetId="tgt" initialSearch={normalizeLogRouteSearch({ from: '2026-01-02T00:00:00Z', to: '2026-01-01T00:00:00Z' })} /></QueryClientProvider>);
    expect(screen.getByRole('alert')).toHaveTextContent('开始时间必须早于结束时间');
    expect(request).not.toHaveBeenCalled();
  });
});
