/**
 * Web API boundary.
 *
 * All product reads flow through the guarded {@link WebReadonlyClient} facade
 * from `@incidentlens/protocol`, whose every request is routed through the
 * read-only guard. Raw generated clients, the generated SDK internals, and
 * direct `fetch` calls are not allowed in this workspace (see `eslint.config.js`).
 */
import { createWebReadonlyClient } from '@incidentlens/protocol';
import type { WebReadonlyClient } from '@incidentlens/protocol';
import { guardedFetch } from './read-only-guard';

/**
 * Absolute same-origin API root.
 *
 * The generated SDK builds its request URLs by joining this root to each
 * endpoint path and then constructs a `Request`. `Request` rejects relative URLs
 * under Node's fetch (undici), so the web client uses an absolute
 * same-origin root. In the browser this is equivalent to a root-relative
 * `/api/v1`; in the jsdom test environment it resolves to
 * `http://localhost:3000`, which MSW's relative `/api/v1/*` handlers match. When
 * no `window` is present the original root-relative default is kept.
 */
const API_ROOT = typeof window === 'undefined' ? '/api/v1' : window.location.origin + '/api/v1';

function fetchWithoutCrossRealmSignal(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  if (typeof input === 'object' && input !== null && 'url' in input) {
    const request = input as Request;
    return guardedFetch(request.url, { method: request.method, headers: request.headers });
  }
  const safeInit = { ...init };
  delete safeInit.signal;
  return guardedFetch(input, safeInit);
}

/**
 * The read-only client used by the observability web UI.
 *
 * Every HTTP request it issues is a GET routed through {@link guardedFetch},
 * so a regression that attempts a mutation fails fast with
 * `ReadOnlyViolationError`.
 */
export const readonlyClient: WebReadonlyClient = createWebReadonlyClient({
  baseUrl: API_ROOT,
  fetch: fetchWithoutCrossRealmSignal,
});

export type { WebReadonlyClient };
export { ReadOnlyViolationError, READ_ONLY_METHODS } from './read-only-guard';
export { guardedFetch } from './read-only-guard';
