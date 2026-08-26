import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';

const rootNodeModules = resolve(__dirname, '../../node_modules');
const webNodeModules = resolve(__dirname, 'node_modules');

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      { find: '@', replacement: resolve(__dirname, 'src') },
      { find: 'react/jsx-runtime', replacement: resolve(rootNodeModules, 'react', 'jsx-runtime.js') },
      { find: 'react/jsx-dev-runtime', replacement: resolve(rootNodeModules, 'react', 'jsx-dev-runtime.js') },
      { find: 'react', replacement: resolve(rootNodeModules, 'react') },
      { find: 'react-dom', replacement: resolve(rootNodeModules, 'react-dom') },
      {
        find: /^use-sync-external-store\/shim\/with-selector$/,
        replacement: resolve(__dirname, 'src', 'test', 'use-sync-external-store-with-selector.ts'),
      },
    ],
    dedupe: ['react', 'react-dom'],
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['tests/**/*.test.{ts,tsx}'],
    css: false,
    server: {
      deps: {
        inline: true,
      },
    },
  },
});
