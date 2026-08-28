import { expect, test } from '@playwright/test';
import { installCommonRoutes, ids, expectReadOnly } from './fixtures';

test.describe('总览与服务', () => {
  test.beforeEach(async ({ page }) => { await installCommonRoutes(page); });

  test('从总览进入服务详情', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: '总览' })).toBeVisible();
    await page.getByRole('link', { name: ids.service, exact: true }).first().click();
    await expect(page).toHaveURL(new RegExp(`/services/${ids.service}(?:\\?|$)`));
    await expect(page.getByRole('heading', { name: '服务详情' })).toBeVisible();
  });

  test('只读请求不发送 mutation', async ({ page }) => {
    await page.goto('/');
    await expectReadOnly(page);
    await expect(page.locator('button, [role="button"], input, select, textarea')).toHaveCount(0);
  });
});
