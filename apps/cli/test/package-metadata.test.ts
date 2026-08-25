import { readFile } from 'node:fs/promises';
import { expect, it } from 'vitest';

it('publishes the incidentlens executable for supported Node versions', async () => {
  const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  expect(pkg.name).toBe('@incidentlens/cli');
  expect(pkg.bin).toEqual({ incidentlens: './dist/cli.js' });
  expect(pkg.engines).toEqual({ node: '>=22.19.0' });
  expect(pkg.type).toBe('module');
});

it('includes shebang in built CLI entry', async () => {
  const content = await readFile(new URL('../dist/cli.js', import.meta.url), 'utf8');
  expect(content.startsWith('#!/usr/bin/env node')).toBe(true);
});
