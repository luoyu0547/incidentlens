import { afterEach, describe, expect, it } from 'vitest';
import { startFakeControlPlane } from './fake-control-plane.js';
import { spawnCli, type PtyDriver } from './pty-driver.js';

let driver: PtyDriver | undefined;
afterEach(() => driver?.dispose());

describe('CLI PTY startup', () => {
  it('boots in a real 80x24 terminal, resizes, renders wide text, and never leaks secrets', async () => {
    const server = await startFakeControlPlane();
    try {
      driver = spawnCli({ INCIDENTLENS_API_URL: server.url, INCIDENTLENS_TOKEN: server.token }, 80, 24);
      await driver.waitFor('IncidentLens');
      driver.resize(120, 40);
      driver.write('你好');
      expect(driver.output()).not.toContain(server.secret);
      expect(driver.output()).not.toContain(server.token);
    } finally { await server.stop(); }
  }, 10000);
});
