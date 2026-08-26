import { describe, it, expect, beforeEach, vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import { screen, within } from '@testing-library/react';
import type { ServiceDetailView } from '@incidentlens/protocol';

import { server } from '../src/test/server';
import { renderApp } from '../src/test/render-app';

const SERVICE: ServiceDetailView = {
  service_id: 'svc-web',
  status: 'degraded',
  generated_at: '2026-08-26T09:30:00Z',
  last_observed_at: '2026-08-26T09:25:00Z',
  instances: [
    {
      target_id: 'tgt-host-a',
      target_name: 'host-a',
      host: 'host-a.internal',
      status: 'degraded',
      container_names: ['web-1', 'web-2'],
      last_tested_at: '2026-08-26T09:20:00Z',
      last_observed_at: '2026-08-26T09:25:00Z',
      pending_approval_count: 1,
      issue_ids: ['iss-1'],
      investigation_ids: ['inv-1'],
    },
  ],
  target_ids: ['tgt-host-a'],
  issue_ids: ['iss-1'],
  investigation_ids: ['inv-1'],
  log_sources: [],
  pending_approval_count: 1,
};

function useService(data: ServiceDetailView | null, status = 200) {
  server.use(
    http.get('http://localhost:3000/api/v1/services/:serviceId', () =>
      HttpResponse.json(data, { status }),
    ),
  );
}

beforeEach(() => {
  server.resetHandlers();
});

describe('Service page', () => {
  it('renders generated service facts without exposing unsafe target configuration', async () => {
    useService(SERVICE);
    renderApp({ initialEntries: ['/services/svc-web'] });

    const facts = await screen.findByRole('region', { name: '服务状态' });
    expect(within(facts).getByText('svc-web')).toBeVisible();
    expect(within(facts).getAllByText('降级')).not.toHaveLength(0);
    expect(within(facts).getByText('tgt-host-a')).toBeVisible();
    expect(within(facts).getByText('host-a')).toBeVisible();
    expect(within(facts).getByText('host-a.internal')).toBeVisible();
    expect(within(facts).getByText('web-1、web-2')).toBeVisible();
    expect(document.querySelector('time[datetime="2026-08-26T09:20:00Z"]')).not.toBeNull();
    expect(document.querySelector('time[datetime="2026-08-26T09:25:00Z"]')).not.toBeNull();
    expect(document.body.textContent).not.toContain('authentication_hint');
  });

  it('links issue and investigation relationships to their read routes', async () => {
    useService(SERVICE);
    renderApp({ initialEntries: ['/services/svc-web'] });

    const issues = await screen.findByRole('region', { name: '关联问题' });
    expect(within(issues).getByRole('link', { name: 'iss-1' })).toHaveAttribute('href', '/issues/iss-1');

    const investigations = screen.getByRole('region', { name: '关联调查' });
    expect(within(investigations).getByRole('link', { name: 'inv-1' })).toHaveAttribute(
      'href',
      '/investigations/inv-1',
    );
  });

  it('keeps pending approval as a CLI operator decision without mutation controls', async () => {
    useService(SERVICE);
    renderApp({ initialEntries: ['/services/svc-web'] });

    await screen.findByRole('region', { name: '服务状态' });
    expect(screen.getByText('等待 CLI 中的操作者决策')).toBeVisible();
    expect(screen.queryByRole('button', { name: /approve|reject|restart|rollback|edit|shell/i })).toBeNull();
  });

  it('attempts the log WebSocket connection from the service route', async () => {
    const socketUrls: string[] = [];
    class ObservedWebSocket {
      static readonly OPEN = 1;
      readonly readyState = ObservedWebSocket.OPEN;
      onopen: (() => void) | null = null;
      onclose: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      constructor(url: string) {
        socketUrls.push(url);
      }
      send() {}
      close() { this.onclose?.(); }
    }
    vi.stubGlobal('WebSocket', ObservedWebSocket);
    useService(SERVICE);
    renderApp({ initialEntries: ['/services/svc-web?mode=live'] });

    await screen.findByRole('region', { name: '日志查看器' });
    expect(socketUrls.some((url) => url.endsWith('/ws/v1/logs'))).toBe(true);
    vi.unstubAllGlobals();
  });
});
