/**
 * IncidentLens CLI entry point.
 *
 * Validates Node version, handles --version, and renders the App.
 */

import React from 'react';
import { render } from 'ink';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { FileConfigStore } from './config/file-config-store.js';
import { KeyringTokenStore } from './auth/keyring-token-store.js';
import { ControlPlaneApi } from './api/control-plane-api.js';
import { WsEventStream } from './stream/ws-event-stream.js';
import { App } from './app/App.js';

// Check Node version
const nodeVersion = process.version;
const [major] = nodeVersion.slice(1).split('.').map(Number);

if (major < 22 || (major === 22 && Number(nodeVersion.split('.')[1]) < 19)) {
  console.error(`IncidentLens requires Node.js >= 22.19.0. Current version: ${nodeVersion}`);
  process.exit(1);
}

// Handle --version
const args = process.argv.slice(2);
if (args.includes('--version')) {
  console.log('0.1.0');
  process.exit(0);
}

// Create dependencies
const configStore = new FileConfigStore(join(homedir(), '.incidentlens'));
const tokenStore = new KeyringTokenStore();
const api = new ControlPlaneApi({
  baseUrl: process.env['INCIDENTLENS_API_URL'] ?? 'http://localhost:8000',
  token: process.env['INCIDENTLENS_TOKEN'],
});

const eventStream = new WsEventStream({
  baseUrl: process.env['INCIDENTLENS_API_URL'] ?? 'http://localhost:8000',
  token: process.env['INCIDENTLENS_TOKEN'] ?? '',
});

// Render app. Shutdown aborts the stream through App's effect cleanup and
// intentionally does not call the server cancel endpoint.
render(
  React.createElement(App, {
    dependencies: {
      api,
      configStore,
      tokenStore,
      eventStream: eventStream as never,
      now: () => new Date(),
      exit: () => undefined,
    },
  }),
);

