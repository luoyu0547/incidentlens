import '@testing-library/jest-dom/vitest';
import { afterAll, afterEach, beforeAll } from 'vitest';
import { server } from './server';

// Deterministic timezone for tests
process.env.TZ = 'UTC';

// jsdom does not implement window.scrollTo; TanStack Router's scroll
// restoration calls it during navigation. Stub it to keep test output clean.
Object.defineProperty(window, 'scrollTo', {
  value: () => {
    /* no-op */
  },
  writable: true,
});

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
