import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  retries: 0,
  use: {
    baseURL: 'http://localhost:4173',
    trace: 'on-first-retry',
    // macOS 13 cannot run Playwright's bundled Chromium; use the installed
    // Chrome channel by default there while keeping CI/Linux on the bundle.
    ...((process.env.PLAYWRIGHT_CHANNEL || (process.platform === 'darwin' ? 'chrome' : undefined))
      ? { channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome' }
      : {}),
  },
  webServer: {
    command: 'npm run build && npm run preview',
    port: 4173,
    reuseExistingServer: !process.env.CI,
  },
});
