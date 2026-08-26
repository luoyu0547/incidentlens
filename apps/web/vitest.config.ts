import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';

const webNodeModules = resolve(__dirname, 'node_modules');

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      { find: '@', replacement: resolve(__dirname, 'src') },
      { find: 'react', replacement: resolve(webNodeModules, 'react') },
      { find: 'react-dom', replacement: resolve(webNodeModules, 'react-dom') },
      { find: 'react/jsx-runtime', replacement: resolve(webNodeModules, 'react', 'jsx-runtime.js') },
      { find: 'react/jsx-dev-runtime', replacement: resolve(webNodeModules, 'react', 'jsx-dev-runtime.js') },
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
