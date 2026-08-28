import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';

// Local demo convenience only: the dev server can attach the already-running
// control-plane bearer token server-side, so the browser never needs to see or
// type credentials. This is intentionally absent from the production bundle.
const devAuthHeaders = process.env.INCIDENTLENS_TOKEN
  ? { Authorization: `Bearer ${process.env.INCIDENTLENS_TOKEN}` }
  : undefined;

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        headers: devAuthHeaders,
      },
      '/events': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        headers: devAuthHeaders,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
        headers: devAuthHeaders,
      },
    },
  },
  build: {
    outDir: '../control-plane/src/incidentlens_control_plane/static/web',
    emptyOutDir: true,
    manifest: true,
    sourcemap: true,
  },
});
