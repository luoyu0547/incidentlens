import { expect, test } from '@playwright/test';
import { installCommonRoutes, ids, installLogSocket, log, logPage, routeJson } from './fixtures';

test.describe('日志流恢复', () => {
  test('断线后回补 c11-c15，重连 c15，c16 只出现一次', async ({ page }) => {
    let disconnected = false;
    let socketCount = 0;
    await installCommonRoutes(page);
    await routeJson(page, `/services/${ids.service}/logs`, logPage([log(10)], null));
    await page.routeWebSocket('**/api/v1/services/*/logs/stream', (socket) => {
      socketCount += 1;
      socket.onMessage((raw) => {
        const message = JSON.parse(String(raw)) as { action?: string };
        if (message.action === 'subscribe' && socketCount === 1 && !disconnected) {
          disconnected = true;
          socket.close();
        } else if (message.action === 'subscribe' && socketCount > 1) {
          socket.send(JSON.stringify({ event_type: 'log.record', cursor: 'c15', payload: log(15) }));
          socket.send(JSON.stringify({ event_type: 'log.record', cursor: 'c16', payload: log(16) }));
        }
      });
    });
    await page.route('**/api/v1/services/*/logs**', async (route) => {
      const url = new URL(route.request().url());
      const before = url.searchParams.get('before');
      const items = before === 'c10' ? [log(11), log(12), log(13), log(14), log(15)] : [log(10)];
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(logPage(items, null)) });
    });
    await page.goto(`/services/${ids.service}`);
    await expect(page.getByText('c16')).toHaveCount(1, { timeout: 10_000 });
    await expect(page.getByText('c15')).toHaveCount(1);
  });

  test('收到 gap 先 HTTP resync 再恢复 live', async ({ page }) => {
    let gapSeen = false;
    await installCommonRoutes(page);
    await routeJson(page, `/services/${ids.service}/logs`, logPage([log(10)], null));
    await installLogSocket(page, (message, socket) => {
      if (message.action === 'subscribe' && !gapSeen) { gapSeen = true; socket.send(JSON.stringify({ event_type: 'stream.gap', cursor: 'c10' })); }
    });
    await page.goto(`/services/${ids.service}`);
    await expect(page.getByText('c10')).toBeVisible();
    expect(gapSeen).toBe(true);
  });
});
