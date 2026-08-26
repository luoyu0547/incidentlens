import { expect, test } from '@playwright/test';
import { installCommonRoutes, ids, installLogSocket, log, logEvent, logPage, routeJson } from './fixtures';

test.describe('日志流恢复', () => {
  test('断线后回补 gap，重连并避免重复记录', async ({ page }) => {
    let socketCount = 0;
    let disconnected = false;
    const backfillRequests: string[] = [];
    await installCommonRoutes(page);
    await routeJson(page, `/services/${ids.service}/logs`, logPage([log(10)], null));
    await page.route('**/api/v1/services/*/logs**', async (route) => {
      const url = new URL(route.request().url());
      backfillRequests.push(url.search);
      const items = url.searchParams.get('before') === 'c10' ? [log(11), log(12), log(13), log(14), log(15)] : [log(10)];
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(logPage(items, null)) });
    });
    await page.routeWebSocket('**/ws/v1/logs', (socket) => {
      socketCount += 1;
      socket.onMessage((raw) => {
        const message = JSON.parse(String(raw)) as { action?: string };
        if (message.action !== 'subscribe') return;
        if (socketCount === 1 && !disconnected) {
          disconnected = true;
          socket.close();
          return;
        }
        socket.send(JSON.stringify(logEvent('log.subscribed', 'c15', { service_id: ids.service })));
        socket.send(JSON.stringify(logEvent('log.record', 'c15', log(15))));
        socket.send(JSON.stringify(logEvent('log.record', 'c16', log(16, 'recovered live'))));
      });
    });
    await page.goto(`/services/${ids.service}?mode=live`);
    await expect(page.getByText('正在重新连接日志流…')).toBeVisible();
    await expect(page.getByText('recovered live')).toHaveCount(1, { timeout: 10_000 });
    await expect(page.getByText('日志流已连接')).toBeVisible();
    expect(socketCount).toBeGreaterThanOrEqual(2);
    expect(backfillRequests.some((query) => query.includes('before=c10'))).toBe(true);
    await expect(page.getByText('c15')).toHaveCount(1);
  });

  test('收到 gap 显示恢复状态并执行一次权威回补', async ({ page }) => {
    let gapSeen = false;
    const historyRequests: string[] = [];
    await installCommonRoutes(page);
    await routeJson(page, `/services/${ids.service}/logs`, logPage([log(10)], null));
    await page.route('**/api/v1/services/*/logs**', async (route) => {
      historyRequests.push(route.request().url());
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(logPage([log(10), log(11, 'authoritative')], null)) });
    });
    await installLogSocket(page, (message, socket) => {
      if (message.action === 'subscribe' && !gapSeen) {
        gapSeen = true;
        socket.send(JSON.stringify(logEvent('stream.gap', 'c10')));
      } else if (message.action === 'subscribe') {
        socket.send(JSON.stringify(logEvent('log.subscribed', 'c11', { service_id: ids.service })));
      }
    });
    await page.goto(`/services/${ids.service}?mode=live`);
    await expect(page.getByText('正在恢复日志间隙…')).toBeVisible();
    await expect(page.getByText('authoritative')).toBeVisible();
    await expect(page.getByText('日志流已连接')).toBeVisible();
    expect(gapSeen).toBe(true);
    expect(historyRequests).toHaveLength(2);
  });

  test('slow consumer backpressure 仍保持只读协议', async ({ page }) => {
    const frames: Record<string, unknown>[] = [];
    await installCommonRoutes(page);
    await routeJson(page, `/services/${ids.service}/logs`, logPage([log(10)], null));
    await installLogSocket(page, (message, socket) => {
      if (message.action === 'subscribe') {
        socket.send(JSON.stringify(logEvent('stream.slow_consumer', 'c10', { action: 'ack' })));
        socket.send(JSON.stringify(logEvent('log.subscribed', 'c10', { service_id: ids.service })));
      }
    });
    page.on('websocket', (socket) => socket.on('framesent', (frame) => {
      try { frames.push(JSON.parse(String(frame)) as Record<string, unknown>); } catch { /* ignore */ }
    }));
    await page.goto(`/services/${ids.service}?mode=live`);
    await expect(page.getByText('日志流已连接')).toBeVisible();
    expect(frames.every((frame) => ['subscribe', 'ack', 'pause', 'resume', 'update'].includes(String(frame.action)))).toBe(true);
  });
});
