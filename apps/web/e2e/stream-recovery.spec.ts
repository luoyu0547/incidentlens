import { expect, test } from '@playwright/test';
import { cursors, installCommonRoutes, ids, installLogSocket, log, logEvent, logPage } from './fixtures';

test.describe('日志流恢复', () => {
  test('断线后以合法 opaque cursor 回补、重连并避免重复记录', async ({ page }) => {
    let socketCount = 0;
    let closeFirstSocket!: () => void;
    const socketClosed = new Promise<void>((resolve) => { closeFirstSocket = resolve; });
    const historyRequests: URL[] = [];
    await installCommonRoutes(page);
    await page.route('**/api/v1/services/svc-web/logs**', async (route) => {
      const url = new URL(route.request().url());
      historyRequests.push(url);
      const items = url.searchParams.get('before') === cursors[10]
        ? [log(11), log(12), log(13), log(14), log(15)] : [log(10)];
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(logPage(items, null)) });
    });
    await page.routeWebSocket('**/ws/v1/logs', (socket) => {
      socketCount += 1;
      socket.onMessage((raw) => {
        const message = JSON.parse(String(raw)) as { action?: string };
        if (message.action !== 'subscribe') return;
        if (socketCount === 1) { socket.close(); closeFirstSocket(); return; }
        socket.send(JSON.stringify(logEvent('log.subscribed', cursors[15], { service_id: ids.service })));
        socket.send(JSON.stringify(logEvent('log.record', cursors[15], log(15))));
        socket.send(JSON.stringify(logEvent('log.record', cursors[16], log(16, 'recovered live'))));
      });
    });
    await page.goto(`/services/${ids.service}?mode=live`);
    await socketClosed;
    await expect(page.getByText('正在重新连接日志流…')).toBeVisible();
    await expect(page.getByText('recovered live')).toHaveCount(1, { timeout: 10_000 });
    await expect(page.getByText('日志流已连接')).toBeVisible();
    expect(socketCount).toBe(2);
    expect(historyRequests).toHaveLength(2);
    expect(historyRequests[1].searchParams.get('before')).toBe(cursors[10]);
    await expect(page.locator('[data-log-id="log-15"]')).toHaveCount(1);
  });

  test('收到 gap 后只执行一次不带 before 的权威回补', async ({ page }) => {
    let gapSent = false;
    let releaseGap!: () => void;
    const gapReleased = new Promise<void>((resolve) => { releaseGap = resolve; });
    const historyRequests: URL[] = [];
    await installCommonRoutes(page);
    await page.route('**/api/v1/services/svc-web/logs**', async (route) => {
      const url = new URL(route.request().url());
      historyRequests.push(url);
      const isInitial = historyRequests.length === 1;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(logPage(
        isInitial ? [log(10)] : [log(10), log(11, 'authoritative')], null,
      )) });
    });
    await installLogSocket(page, (message, socket) => {
      if (message.action === 'subscribe' && !gapSent) {
        gapSent = true;
        void gapReleased.then(() => socket.send(JSON.stringify(logEvent('stream.gap', cursors[10]))));
      } else if (message.action === 'subscribe') {
        socket.send(JSON.stringify(logEvent('log.subscribed', cursors[11], { service_id: ids.service })));
      }
    });
    await page.goto(`/services/${ids.service}?mode=live`);
    await expect.poll(() => gapSent).toBe(true);
    expect(historyRequests).toHaveLength(1);
    releaseGap();
    await expect(page.getByText('正在恢复日志间隙…')).toBeVisible();
    await expect(page.getByText('authoritative')).toBeVisible();
    await expect(page.getByText('日志流已连接')).toBeVisible();
    expect(historyRequests).toHaveLength(2);
    expect(historyRequests[0].searchParams.get('before')).toBeNull();
    expect(historyRequests[1].searchParams.get('before')).toBeNull();
  });

  test('slow consumer 顶层 cursor 为空时从 payload.last_cursor 单独 ack', async ({ page }) => {
    const frames: Record<string, unknown>[] = [];
    let releaseSlowConsumer!: () => void;
    const slowConsumerReleased = new Promise<void>((resolve) => { releaseSlowConsumer = resolve; });
    await installCommonRoutes(page);
    await page.route('**/api/v1/services/svc-web/logs**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(logPage([log(10)], null)) }));
    await installLogSocket(page, (message, socket) => {
      if (message.action === 'subscribe') {
        socket.send(JSON.stringify(logEvent('log.subscribed', cursors[10], { service_id: ids.service })));
        void slowConsumerReleased.then(() => socket.send(JSON.stringify(logEvent('stream.slow_consumer', null, { action: 'ack', last_cursor: cursors[10] }))));
      }
    });
    page.on('websocket', (socket) => socket.on('framesent', (frame) => {
      try { frames.push(JSON.parse(String(frame)) as Record<string, unknown>); } catch { /* ignore */ }
    }));
    await page.goto(`/services/${ids.service}?mode=live`);
    await expect(page.getByText('日志流已连接')).toBeVisible();
    frames.length = 0;
    releaseSlowConsumer();
    await expect.poll(() => frames).toEqual([{ action: 'ack', cursor: cursors[10] }]);
    expect(frames.every((frame) => ['subscribe', 'ack', 'pause', 'resume', 'update'].includes(String(frame.action)))).toBe(true);
  });
});
