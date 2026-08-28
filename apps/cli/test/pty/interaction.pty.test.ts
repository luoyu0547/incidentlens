import { afterEach, describe, expect, it } from 'vitest';
import { startFakeControlPlane } from './fake-control-plane.js';
import { spawnCli, type PtyDriver } from './pty-driver.js';
let driver: PtyDriver | undefined;
afterEach(() => driver?.dispose());

describe('CLI PTY interaction', () => {
  it('accepts natural language and exits via Ctrl+C without cancellation', async () => {
    const server = await startFakeControlPlane();
    try {
      driver = spawnCli({ INCIDENTLENS_API_URL: server.url, INCIDENTLENS_TOKEN: server.token });
      await driver.waitFor('IncidentLens');
      driver.write('你好，检查服务\r');
      await new Promise((resolve) => setTimeout(resolve, 150));
      driver.write('');
      await driver.waitForExit();
      expect(server.cancelCalls).toHaveLength(0);
      expect(driver.output()).not.toContain(server.token);
    } finally { await server.stop(); }
  }, 10000);

  it('supports escape and command input without exposing auth', async () => {
    const server = await startFakeControlPlane();
    try {
      driver = spawnCli({ INCIDENTLENS_API_URL: server.url, INCIDENTLENS_TOKEN: server.token });
      await driver.waitFor('IncidentLens');
      driver.write('/'); driver.write(''); driver.write(''); driver.write('');
      await driver.waitForExit();
      expect(driver.output()).not.toContain('Authorization');
    } finally { await server.stop(); }
  }, 10000);
});
