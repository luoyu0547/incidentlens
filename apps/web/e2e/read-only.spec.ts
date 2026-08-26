import { expect, test } from '@playwright/test';

const forbiddenControl = /approve|reject|execute|run|restart|stop|delete|edit|rollback|apply|deploy|open shell/i;
const routes = ['/', '/services/svc-web', '/issues', '/issues/iss-1', '/investigations/inv-1'];

const json = (body: unknown) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
const emptyPage = { has_more: false, next_cursor: null, items: [] };

test.describe('read-only web boundary', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/api/v1/**', async (route) => {
      const url = route.request().url();
      if (url.endsWith('/overview')) return route.fulfill(json({ generated_at: new Date().toISOString(), open_issue_count: 0, active_investigation_count: 0, pending_approval_count: 0, service_counts: { healthy: 0, degraded: 0 }, targets: [], recent_resolutions: [] }));
      if (url.endsWith('/issues')) return route.fulfill(json(emptyPage));
      if (url.endsWith('/investigations')) return route.fulfill(json(emptyPage));
      if (url.includes('/logs')) return route.fulfill(json({ ...emptyPage, previous_cursor: null, snapshot_cursor: null }));
      return route.fulfill(json({ service_id: 'svc-web', status: 'healthy', generated_at: new Date().toISOString(), instances: [], investigation_ids: [], issue_ids: [], target_ids: [], log_sources: [], pending_approval_count: 0 }));
    });
  });

  test('shows no mutation controls on every read-only route', async ({ page }) => {
    for (const route of routes) {
      await page.goto(route);
      await expect(page.locator('button, [role="button"], input, select, textarea')).not.toContainText(forbiddenControl);
      await expect(page.locator('a')).not.toContainText(forbiddenControl);
    }
  });

  test('issues only read HTTP methods and allowed log websocket commands', async ({ page }) => {
    const methods: string[] = [];
    const frames: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('/api/')) methods.push(request.method());
    });
    page.on('websocket', (socket) => socket.on('framesent', (frame) => frames.push(String(frame))));

    await page.goto('/services/svc-web');
    await page.waitForLoadState('networkidle');
    expect(methods).toEqual(expect.arrayContaining(['GET']));
    expect(methods.every((method) => ['GET', 'HEAD', 'OPTIONS'].includes(method))).toBe(true);
    expect(frames.every((frame) => {
      try {
        return ['subscribe', 'update', 'pause', 'resume', 'ack'].includes((JSON.parse(frame) as { action?: string }).action ?? '');
      } catch {
        return false;
      }
    })).toBe(true);
  });
});
