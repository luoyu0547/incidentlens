import { defineConfig } from 'tsup';

export default defineConfig({
  entry: ['src/cli.tsx'],
  format: ['esm'],
  outDir: 'dist',
  clean: true,
  sourcemap: true,
  bundle: true,
  splitting: false,
  platform: 'node',
  target: 'node22',
  banner: {
    js: '#!/usr/bin/env node',
  },
  external: [
    // Native modules should not be bundled; npm installs the platform binary
    // through the optional dependency when available.
    /^@napi-rs\/.*$/,
  ],
});
