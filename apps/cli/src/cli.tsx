/**
 * IncidentLens CLI entry point.
 *
 * Validates Node version, handles --version, and renders the App.
 */

import React from 'react';
import { render } from 'ink';
import { FileConfigStore } from './config/file-config-store.js';
import { KeyringTokenStore } from './auth/keyring-token-store.js';
import { ControlPlaneApi } from './api/control-plane-api.js';

// Check Node version
const nodeVersion = process.version;
const [major] = nodeVersion.slice(1).split('.').map(Number);

if (major < 22) {
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
const configStore = new FileConfigStore();
const tokenStore = new KeyringTokenStore();
const api = new ControlPlaneApi({
  baseUrl: process.env['INCIDENTLENS_API_URL'] ?? 'http://localhost:8000',
  token: process.env['INCIDENTLENS_TOKEN'],
});

// Simple placeholder component for Task 3-4 verification
function IncidentLens() {
  return React.createElement('ink-text', null, 'IncidentLens CLI - Task 3-4 Complete');
}

// Render app
render(React.createElement(IncidentLens));
