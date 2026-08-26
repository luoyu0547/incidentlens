import { expect, test } from '@playwright/test';
import { installCommonRoutes, ids, installLogSocket, log, logEvent, logPage, routeJson } from './fixtures';

test.describe('服务日志', () => {
  test.beforeEach(async ({ page }) => {
    await installCommonRoutes(page);
    await routeJson(page, `/services/${ids.service}/logs`, logPage([log(1, 'oldest'), log(2, 'latest')], 'before-1'));
  });

  test('service golden path uses historical GET and real log WebSocket', async ({ page }) => {
    const methods: string[] = [];
    const sockets: string[] = [];
    page.on('request', (request) => { if (request.url().includes('/api/v1/')) methods.push(request.method()); });
    page.on('websocket', (socket) => sockets.push(socket.url()));
    await installLogSocket(page, (message, socket) => {
      if (message.action === 'subscribe') {
        expect(message).toMatchObject({ action: 'subscribe', service_id: ids.service, target_id: ids.target, cursor: 'c2' });
        void socket.send(JSON.stringify(logEvent('log.subscribed', 'c2', { service_id: ids.service })));
        void socket.send(JSON.stringify(logEvent('log.record', 'c3', log(3, 'live record'))));
      }
    });
    await page.goto(`/services/${ids.service}?mode=live&target=${ids.target}&q=timeout`);
    await expect(page.getByRole('heading', { name: '服务详情' })).toBeVisible();
    await expect(page.getByText('latest')).toBeVisible();
    await expect(page.getByText('live record')).toBeVisible();
    await expect(page.getByText('日志流已连接')).toBeVisible();
    expect(sockets.some((url) => url.endsWith('/ws/v1/logs'))).toBe(true);
    expect(methods.every((method) => ['GET', 'HEAD', 'OPTIONS'].includes(method))).toBe(true);

    await page.getByLabel('日志级别').selectOption('error');
    await page.getByLabel('日志搜索').fill('connection');
    expect(methods.every((method) => ['GET', 'HEAD', 'OPTIONS'].includes(method))).toBe(true);
  });

  test('历史日志支持筛选、结构化数据和证据定位', async ({ page }) => {
    await routeJson(page, `/services/${ids.service}/logs`, logPage([
      log(1, 'oldest'), log(2, 'latest', { fields: { redacted: true }, structured_json: { code: 503 } }),
    ], null));
    await page.goto(`/services/${ids.service}?mode=history&evidence=${ids.evidence}`);
    await expect(page.getByText('latest')).toBeVisible();
    await expect(page.getByText('已脱敏')).toBeVisible();
    await page.getByText('latest').click();
    await expect(page.getByText(/code/)).toBeVisible();
    await page.getByRole('button', { name: '定位日志 log-2' }).click();
    await expect(page.getByRole('button', { name: '定位日志 log-2' })).toBeVisible();
  });

  test('可加载更早日志且保留当前视口', async ({ page }) => {
    await page.goto(`/services/${ids.service}?mode=history`);
    const button = page.getByRole('button', { name: '加载更早日志' });
    await expect(button).toBeVisible();
    await button.click();
    await expect(page.getByText('oldest')).toBeVisible();
  });
});
