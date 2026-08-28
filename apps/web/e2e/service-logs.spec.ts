import { expect, test } from '@playwright/test';
import { cursors, installCommonRoutes, ids, installLogSocket, log, logEvent, logPage } from './fixtures';

test.describe('服务日志', () => {
  test('service golden path uses the historical GET and real log WebSocket', async ({ page }) => {
    const methods: string[] = [];
    const logRequests: URL[] = [];
    const sockets: string[] = [];
    await installCommonRoutes(page);
    await page.route('**/api/v1/services/svc-web/logs**', async (route) => {
      const url = new URL(route.request().url());
      logRequests.push(url);
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(logPage([
        log(1, 'oldest'), log(2, 'latest', { fields: { redacted: true } }),
      ], null)) });
    });
    page.on('request', (request) => { if (request.url().includes('/api/v1/')) methods.push(request.method()); });
    await installLogSocket(page, (message, socket) => {
      if (message.action === 'subscribe') {
        expect(message).toMatchObject({ action: 'subscribe', service_id: ids.service, target_id: ids.target, cursor: cursors[2] });
        socket.send(JSON.stringify(logEvent('log.subscribed', cursors[2], { service_id: ids.service })));
        socket.send(JSON.stringify(logEvent('log.record', cursors[3], log(3, 'live record'))));
      }
    }, (url) => sockets.push(url));
    await page.goto(`/services/${ids.service}?mode=live&target=${ids.target}`);
    await expect(page.getByRole('heading', { name: '服务详情' })).toBeVisible();
    await expect(page.getByText('latest')).toBeVisible();
    await expect(page.getByText('已脱敏')).toBeVisible();
    await expect(page.getByText('live record')).toBeVisible();
    await expect(page.getByText('日志流已连接')).toBeVisible();
    expect(logRequests).toHaveLength(1);
    expect(logRequests[0].pathname).toBe('/api/v1/services/svc-web/logs');
    expect(logRequests[0].searchParams.get('limit')).toBe('20');
    expect(logRequests[0].searchParams.get('q')).toBeNull();
    expect(sockets.some((url) => url.endsWith('/ws/v1/logs'))).toBe(true);

    await page.getByLabel('日志级别').selectOption('error');
    await expect.poll(() => logRequests.at(-1)?.searchParams.get('severity')).toBe('error');
    expect(methods.every((method) => ['GET', 'HEAD', 'OPTIONS'].includes(method))).toBe(true);
  });

  test('点击 EvidenceMarker 会定位已存在 anchor 对应的日志行', async ({ page }) => {
    await installCommonRoutes(page);
    await page.route('**/api/v1/services/svc-web/logs**', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(logPage([
        log(1, 'oldest'),
        log(2, 'latest', { fields: { redacted: true, structured_json: { code: 503 } } }),
      ], null)) });
    });
    await page.goto(`/services/${ids.service}?mode=history&anchor=log-2&cursor=${encodeURIComponent(cursors[2])}`);
    await expect(page.getByRole('status', { name: '已定位日志 log-2' })).toBeVisible();
    await page.getByText('latest').click();
    await expect(page.getByText('code')).toBeVisible();
    await expect(page.getByText('503')).toBeVisible();
    const anchored = page.locator('[data-log-id="log-2"]');
    await expect(anchored).toBeInViewport();
    await page.getByRole('button', { name: '定位日志 log-2' }).click();
    await expect(anchored).toBeInViewport();
  });

  test('可加载更早日志且保留当前视口', async ({ page }) => {
    await installCommonRoutes(page);
    await page.route('**/api/v1/services/svc-web/logs**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(logPage([log(1, 'oldest')], cursors[1])) }));
    await page.goto(`/services/${ids.service}?mode=history`);
    const button = page.getByRole('button', { name: '加载更早日志' });
    await expect(button).toBeVisible();
    await button.click();
    await expect(page.getByText('oldest')).toBeVisible();
  });
});
