/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * Vite build / dev-server configuration for the chess SPA, with an embedded
 * Vitest test configuration (the project ships a single config file — Vitest
 * reads its settings from the `test` key here, so the triple-slash reference
 * above augments Vite's typed config with that key).
 *
 * Dev-server proxy (load-bearing): the frontend talks to the FastAPI backend
 * exclusively through relative paths, so the dev server forwards:
 *   - `/ws`  → http://localhost:8000 with `ws: true` (proxies the WebSocket
 *              upgrade) for the `/ws/game` and `/ws/multiplayer` channels, and
 *   - `/api` → http://localhost:8000 for the REST surface (health, initial load).
 * Only these two prefixes are proxied, with no path rewriting, so the same
 * relative URLs resolve to the backend in development and to the co-served
 * origin in production.
 *
 * Production build output stays at the default `dist/`, which the backend
 * serves as static files (with SPA fallback) for `make start`.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    // Vite's default dev-server port; the proxy target (backend) is on 8000.
    port: 5173,
    proxy: {
      // Proxy the WebSocket routes (/ws/game, /ws/multiplayer); `ws: true`
      // forwards the HTTP→WebSocket upgrade handshake to the backend.
      '/ws': {
        target: 'http://localhost:8000',
        ws: true,
        changeOrigin: true,
      },
      // Proxy the REST surface (health checks, initial load) to the backend.
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    // Emit to frontend/dist; the backend static-serve and .gitignore depend on this.
    outDir: 'dist',
  },
  test: {
    // Expose describe/it/expect as globals (pairs with the `vitest/globals`
    // type entry in tsconfig.json) so test files need no per-file imports.
    globals: true,
    // Component and hook suites need a DOM; jsdom is a declared dev dependency.
    environment: 'jsdom',
    // Side-effect setup: registers @testing-library/jest-dom matchers and
    // performs React Testing Library cleanup after each test.
    setupFiles: ['./src/tests/setup.ts'],
  },
});
