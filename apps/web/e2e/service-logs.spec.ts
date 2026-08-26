import { expect, test } from '@playwright/test';
import { installCommonRoutes, ids, log, logPage, routeJson } from './fixtures';

test.describe('服务日志', () => {
  test.beforeEach(async ({ page }) => {
    await installCommonRoutes(page);
    await routeJson(page, `/services/${ids.service}/logs`, logPage([log(1, 'oldest'), log(2, 'latest')], 'before-1'));
  });

  test('保留 URL 筛选并支持历史分页', async ({ page }) => {
    const requests: string[] = [];
    page.on('request', (request) => { if (request.url().includes('/logs')) requests.push(request.url()); });
    await page.goto(`/services/${ids.service}?q=timeout&level=error&follow=false`);
    await expect(page.getByRole('heading', { name: '服务详情' })).toBeVisible();
    expect(requests.some((url) => url.includes('q=timeout') || url.includes('level=error'))).toBeTruthy();
    await expect(page.getByRole('region', { name: '日志查看器' })).toBeVisible();
  });

  test('可加载更早日志且保留当前视口', async ({ page }) => {
    await page.goto(`/services/${ids.service}?mode=history`);
    const button = page.getByRole('button', { name: '加载更早日志' });
    await expect(button).toBeVisible();
    await button.click();
    await expect(page.getByText('oldest')).toBeVisible();
  });
});
