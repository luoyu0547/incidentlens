import { expect, test } from '@playwright/test';
import { installCommonRoutes, ids, log, logPage, routeJson } from './fixtures';

test.describe('证据导航', () => {
  test('Issue → Evidence → 日志定位并可浏览器返回', async ({ page }) => {
    await installCommonRoutes(page);
    await routeJson(page, `/services/${ids.service}/logs`, logPage([log(10, 'evidence context')], null));
    await page.goto(`/issues/${ids.issue}`);
    await expect(page.getByRole('heading', { name: '问题详情' })).toBeVisible();
    await page.getByRole('button', { name: '查看已脱敏证据' }).click();
    await expect(page.getByText('脱敏日志')).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL(`/issues/${ids.issue}`);
  });
});
