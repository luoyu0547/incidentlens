import { expect, test } from '@playwright/test';
import { ids, installLogSocket, log, logEvent, logPage } from './fixtures';

const forbiddenControl = /approve|reject|execute|run|restart|stop|delete|edit|rollback|apply|deploy|open shell/i;
const routes = ['/', '/services/svc-web', '/issues', '/issues/iss-1', '/investigations/inv-1'];

const json = (body: unknown) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
const emptyPage = { has_more: false, next_cursor: null, items: [] };

test.describe('read-only web boundary', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/overview')) return route.fulfill(json({ generated_at: new Date().toISOString(), open_issue_count: 0, active_investigation_count: 0, pending_approval_count: 0, service_counts: { healthy: 0, degraded: 0 }, targets: [], recent_resolutions: [] }));
      if (url.pathname.endsWith('/issues')) return route.fulfill(json(emptyPage));
      if (url.pathname.endsWith('/investigations')) return route.fulfill(json(emptyPage));
      if (url.pathname.includes('/logs')) return route.fulfill(json({ ...emptyPage, previous_cursor: null, snapshot_cursor: null }));
      return route.fulfill(json({ service_id: ids.service, status: 'healthy', generated_at: new Date().toISOString(), instances: [], investigation_ids: [], issue_ids: [], target_ids: [], log_sources: [], pending_approval_count: 0 }));
    });
  });

  test('shows no mutation controls on every read-only route', async ({ page }) => {
    for (const route of routes) {
      await page.goto(route);
      await expect(page.locator('button, [role="button"], input, select, textarea')).not.toContainText(forbiddenControl);
      await expect(page.locator('a')).not.toContainText(forbiddenControl);
    }
  });

  test('actual service history and log WebSocket remain read-only', async ({ page }) => {
    const methods: string[] = [];
    const frames: Record<string, unknown>[] = [];
    const socketUrls: string[] = [];
    page.on('request', (request) => { if (request.url().includes('/api/v1/')) methods.push(request.method()); });
    page.on('websocket', (socket) => {
      socketUrls.push(socket.url());
      socket.on('framesent', (frame) => { try { frames.push(JSON.parse(String(frame)) as Record<string, unknown>); } catch { /* ignore */ } });
    });
    await installLogSocket(page, (message, socket) => {
      if (message.action === 'subscribe') {
        socket.send(JSON.stringify(logEvent('log.subscribed', 'c1', { service_id: ids.service })));
        socket.send(JSON.stringify(logEvent('log.record', 'c2', log(2, 'readonly live'))));
      }
    });
    await page.goto(`/services/${ids.service}?mode=live`);
    await expect(page.getByText('readonly live')).toBeVisible();
    await page.waitForLoadState('networkidle');
    expect(socketUrls.some((url) => url.endsWith('/ws/v1/logs'))).toBe(true);
    expect(methods).toEqual(expect.arrayContaining(['GET']));
    expect(methods.every((method) => ['GET', 'HEAD', 'OPTIONS'].includes(method))).toBe(true);
    expect(frames.length).toBeGreaterThan(0);
    expect(frames.every((frame) => ['subscribe', 'update', 'pause', 'resume', 'ack'].includes(String(frame.action)))).toBe(true);
    expect(methods.some((method) => ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method))).toBe(false);
  });
});
