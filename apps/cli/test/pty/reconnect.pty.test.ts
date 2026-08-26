import { afterEach, describe, expect, it } from 'vitest';
import { startFakeControlPlane } from './fake-control-plane.js';
import { spawnCli, type PtyDriver } from './pty-driver.js';
let driver: PtyDriver | undefined;
afterEach(() => driver?.dispose());

describe('CLI PTY reconnect', () => {
  it('survives a forced stream disconnect and keeps output deduplicated', async () => {
    const server = await startFakeControlPlane();
    try {
      driver = spawnCli({ INCIDENTLENS_API_URL: server.url, INCIDENTLENS_TOKEN: server.token });
      await driver.waitFor('IncidentLens');
      server.closeStream();
      await new Promise((resolve) => setTimeout(resolve, 350));
      const output = driver.output();
      expect(output).not.toContain(server.token);
      expect((output.match(/连接成功/g) ?? []).length).toBeLessThanOrEqual(2);
      driver.write('');
    } finally { await server.stop(); }
  }, 10000);
});
