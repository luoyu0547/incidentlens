import { afterEach, describe, expect, it } from 'vitest';
import { startFakeControlPlane } from './fake-control-plane.js';
import { spawnCli, type PtyDriver } from './pty-driver.js';
let driver: PtyDriver | undefined;
afterEach(() => driver?.dispose());

describe('CLI PTY approvals', () => {
  it('keeps approval path safe and does not issue cancel on terminal close', async () => {
    const server = await startFakeControlPlane();
    try {
      driver = spawnCli({ INCIDENTLENS_API_URL: server.url, INCIDENTLENS_TOKEN: server.token });
      await driver.waitFor('IncidentLens');
      driver.write('a'); driver.write('r'); driver.write('d'); driver.write(''); driver.write('');
      await new Promise((resolve) => setTimeout(resolve, 150));
      driver.dispose();
      expect(server.cancelCalls).toHaveLength(0);
      expect(driver.output()).not.toContain(server.secret);
      expect(driver.output()).not.toContain(server.token);
    } finally { await server.stop(); }
  }, 10000);
});
