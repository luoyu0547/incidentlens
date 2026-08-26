import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  DEFAULT_BASE_URL,
  ReadonlyApiError,
  createWebReadonlyClient,
} from '../src/web-readonly-client.js';
import type { WebReadonlyClient } from '../src/web-readonly-client.js';

// ---------------------------------------------------------------------------
// Read-only facade tests
//
// These run against the generated client-fetch SDK in Node, so request URLs
// are exercised with an explicit absolute origin; the same-origin relative
// default is covered by the Request-subsclass test below.
// ---------------------------------------------------------------------------

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

const logPageBody = {
  has_more: false,
  items: [],
  next_cursor: null,
  previous_cursor: null,
  snapshot_cursor: null,
};

const issuePageBody = {
  has_more: false,
  items: [],
  next_cursor: null,
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('createWebReadonlyClient', () => {
  it('defaults the API root to the same-origin /api/v1', () => {
    expect(DEFAULT_BASE_URL).toBe('/api/v1');
  });

  it('exposes exactly the approved read-only methods', () => {
    const client = createWebReadonlyClient();
    const methods = Object.keys(client).sort();
    expect(methods).toEqual([
      'getEvidence',
      'getInvestigationSummary',
      'getIssue',
      'getOverview',
      'getService',
      'getServiceLogs',
      'listInvestigations',
      'listIssues',
      'listTargetServices',
      'listTargets',
    ]);
  });

  it('never exposes mutation methods', () => {
    const client = createWebReadonlyClient() as unknown as Record<string, unknown>;
    for (const forbidden of [
      'createTarget',
      'deleteTarget',
      'patchTarget',
      'testTarget',
      'createAgentSession',
      'sendAgentMessage',
      'approveApproval',
      'rejectApproval',
      'rollbackChangeset',
      'logout',
      'createSession',
    ]) {
      expect(client[forbidden]).toBeUndefined();
    }
  });

  it('requests the default API root same-origin relative (no absolute URL baked in)', async () => {
    const NativeRequest = globalThis.Request;
    // The generated client builds `new Request(relativeUrl)`, which Node's
    // native Request rejects; subclassing resolves relative URLs against a
    // synthetic origin so we can prove the facade emitted a same-origin path.
    vi.stubGlobal(
      'Request',
      class extends NativeRequest {
        constructor(input: RequestInfo | URL, init?: RequestInit) {
          super(
            typeof input === 'string' ? new URL(input, 'http://origin.test').toString() : input,
            init,
          );
        }
      },
    );

    const fetchMock = vi.fn(async () => jsonResponse({}));
    const client = createWebReadonlyClient({ fetch: fetchMock as unknown as typeof fetch });

    await client.getOverview();

    const input = fetchMock.mock.calls[0]?.[0] as Request;
    expect(input).toBeDefined();
    const url = new URL(input.url);
    expect(url.origin).toBe('http://origin.test');
    expect(url.pathname).toBe('/api/v1/overview');
    expect(url.search).toBe('');
  });

  it('passes service log query parameters and opaque cursors through unchanged', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(logPageBody));
    const client = createWebReadonlyClient({
      baseUrl: 'http://control-plane:8000',
      fetch: fetchMock as unknown as typeof fetch,
    });

    await client.getServiceLogs('svc-1', {
      before: undefined,
      after: 'opaque-cursor-abc_123',
      limit: 50,
      severity: 'error',
      source_ref: 'svc/log',
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const input = fetchMock.mock.calls[0]?.[0] as Request;
    expect(input.url).toBe(
      'http://control-plane:8000/api/v1/services/svc-1/logs' +
        '?after=opaque-cursor-abc_123&limit=50&severity=error&source_ref=svc%2Flog',
    );
  });

  it('passes issue cursors, filters, and limits through unchanged', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(issuePageBody));
    const client = createWebReadonlyClient({
      baseUrl: 'http://control-plane:8000',
      fetch: fetchMock as unknown as typeof fetch,
    });

    await client.listIssues({
      status: 'open',
      target_id: 't-42',
      limit: 20,
      after: 'issue-cursor-z9_9',
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const input = fetchMock.mock.calls[0]?.[0] as Request;
    expect(input.url).toBe(
      'http://control-plane:8000/api/v1/issues?status=open&target_id=t-42&limit=20&after=issue-cursor-z9_9',
    );
  });

  it('passes the AbortSignal through to the underlying request', async () => {
    const controller = new AbortController();
    let captured: Request | undefined;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      captured = input as Request;
      return jsonResponse(issuePageBody);
    });
    const client = createWebReadonlyClient({
      baseUrl: 'http://control-plane:8000',
      fetch: fetchMock as unknown as typeof fetch,
    });

    await client.listIssues({ after: 'cursor' }, controller.signal);

    expect(captured).toBeDefined();
    expect(captured!.signal.aborted).toBe(false);
    controller.abort();
    expect(captured!.signal.aborted).toBe(true);
  });

  it('normalizes API errors without headers, cookies, or tokens', async () => {
    const errorBody = {
      error: { code: 'auth_required', message: 'Authentication required', request_id: 'req-123' },
    };
    const fetchMock = vi.fn(async () =>
      jsonResponse(errorBody, 401, {
        'Set-Cookie': 'session=evil',
        Authorization: 'Bearer leaked-secret',
      }),
    );
    const client = createWebReadonlyClient({
      baseUrl: 'http://control-plane:8000',
      fetch: fetchMock as unknown as typeof fetch,
    });

    const error = await client.getOverview().catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ReadonlyApiError);
    const apiError = error as ReadonlyApiError;
    expect(apiError.name).toBe('ReadonlyApiError');
    expect(apiError.status).toBe(401);
    expect(apiError.code).toBe('auth_required');
    expect(apiError.requestId).toBe('req-123');
    expect(apiError.message).toBe('Authentication required');

    // Headers, cookies, and request metadata never leak onto the error.
    const ownKeys = Object.keys(apiError);
    expect(ownKeys).not.toContain('headers');
    expect(ownKeys).not.toContain('cookies');
    expect(ownKeys).not.toContain('token');
    expect(JSON.stringify(apiError)).not.toContain('evil');
    expect(JSON.stringify(apiError)).not.toContain('leaked-secret');
  });

  it('normalizes network failures without a status', async () => {
    const fetchMock = vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    });
    const client = createWebReadonlyClient({
      baseUrl: 'http://control-plane:8000',
      fetch: fetchMock as unknown as typeof fetch,
    });

    const error = await client.getOverview().catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ReadonlyApiError);
    const apiError = error as ReadonlyApiError;
    expect(apiError.message).toBe('Failed to fetch');
    expect(apiError.status).toBeUndefined();
    expect(apiError.code).toBeUndefined();
  });

  it('rethrows AbortError so callers can detect cancellation', async () => {
    const fetchMock = vi.fn(async () => {
      throw new DOMException('The operation was aborted', 'AbortError');
    });
    const client = createWebReadonlyClient({
      baseUrl: 'http://control-plane:8000',
      fetch: fetchMock as unknown as typeof fetch,
    });

    await expect(client.getOverview()).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('returns the typed read facade for apps/web consumption', () => {
    const client: WebReadonlyClient = createWebReadonlyClient();
    expect(typeof client.getServiceLogs).toBe('function');
    expect(typeof client.listTargets).toBe('function');
  });
});
