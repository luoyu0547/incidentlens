import '@testing-library/jest-dom/vitest';
import { afterAll, afterEach, beforeAll, vi } from 'vitest';
import { server } from './server';

class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, Set<(event: Event) => void>>();

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: Event) => void) {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: (event: Event) => void) {
    this.listeners.get(type)?.delete(listener);
  }

  close() {}

  emitOpen() { this.onopen?.(); }
  emitMessage(data: string, lastEventId = '') { this.onmessage?.({ data, lastEventId } as MessageEvent); }
  emit(type: string, data: string, lastEventId = '') {
    const event = { data, lastEventId } as MessageEvent;
    this.listeners.get(type)?.forEach((listener) => listener(event));
  }
  emitError() { this.onerror?.(); }
}

vi.stubGlobal('EventSource', MockEventSource);

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
afterEach(() => {
  server.resetHandlers();
  MockEventSource.instances.length = 0;
});
afterAll(() => server.close());
