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
 * The read-only client used by the observability web UI.
 *
 * Every HTTP request it issues is a GET routed through {@link guardedFetch},
 * so a regression that attempts a mutation fails fast with
 * `ReadOnlyViolationError`.
 */
export const readonlyClient: WebReadonlyClient = createWebReadonlyClient({
  fetch: guardedFetch,
});

export type { WebReadonlyClient };
export { ReadOnlyViolationError, READ_ONLY_METHODS } from './read-only-guard';
export { guardedFetch } from './read-only-guard';
