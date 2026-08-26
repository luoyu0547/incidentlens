/**
 * Read-only HTTP boundary for the observability web client.
 *
 * The web UI issues only GET, HEAD, and OPTIONS requests. Every fetch that
 * originates in `apps/web` is routed through {@link guardedFetch}, which
 * rejects any other method with {@link ReadOnlyViolationError} before the
 * network is touched.
 */

export const READ_ONLY_METHODS = ['GET', 'HEAD', 'OPTIONS'] as const;

const ALLOWED_METHODS = new Set<string>(READ_ONLY_METHODS);

/**
 * Thrown when code attempts to issue a mutating request through the guarded
 * fetch. Carries the offending method so violations surface in tests and logs.
 */
export class ReadOnlyViolationError extends Error {
  readonly method: string;

  constructor(method: string) {
    super(
      `read-only guard rejected ${method} — the observability web client may only issue ${READ_ONLY_METHODS.join(', ')} requests`,
    );
    this.name = 'ReadOnlyViolationError';
    this.method = method;
  }
}

function resolveMethod(input: RequestInfo | URL, init?: RequestInit): string {
  if (init?.method !== undefined) {
    return init.method.toUpperCase();
  }
  if (typeof Request !== 'undefined' && input instanceof Request) {
    return input.method.toUpperCase();
  }
  return 'GET';
}

/**
 * Fetch wrapper that enforces the read-only boundary.
 *
 * GET, HEAD, and OPTIONS are forwarded to `fetch()` unchanged. Any other
 * method rejects with {@link ReadOnlyViolationError} and never reaches the
 * network. The method is resolved from the init object or, failing that, from
 * the request object itself.
 */
export function guardedFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const method = resolveMethod(input, init);
  if (!ALLOWED_METHODS.has(method)) {
    return Promise.reject(new ReadOnlyViolationError(method));
  }
  return fetch(input, init);
}
