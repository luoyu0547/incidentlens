import { afterEach, describe, expect, it, vi } from 'vitest';
import { createWebReadonlyClient } from '@incidentlens/protocol';
import { readonlyClient, guardedFetch, ReadOnlyViolationError } from '../src/api/client';
import { assertReadOnlyLogAction, READ_ONLY_LOG_ACTIONS } from '../src/api/read-only-guard';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('read-only boundary', () => {
  it('exposes only the approved read-only methods on the web client', () => {
    const methods = Object.keys(readonlyClient).sort();
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
    expect((readonlyClient as unknown as Record<string, unknown>)['createTarget']).toBeUndefined();
  });

  it('never exports raw SDK endpoint functions from the @incidentlens/protocol root', async () => {
    const pkg = await import('@incidentlens/protocol');
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
      expect(pkg).not.toHaveProperty(forbidden);
    }
  });

  it('allows only subscription-control WebSocket actions', () => {
    for (const action of READ_ONLY_LOG_ACTIONS) {
      expect(() => assertReadOnlyLogAction(action)).not.toThrow();
    }
    for (const action of ['approve', 'reject', 'execute', 'delete', 'deploy']) {
      expect(() => assertReadOnlyLogAction(action)).toThrow(ReadOnlyViolationError);
    }
  });

  it('rejects mutation methods with ReadOnlyViolationError before touching the network', async () => {
    const fetchMock = vi.fn(async () => new Response('', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
      await expect(guardedFetch('http://api.test/api/v1/x', { method })).rejects.toBeInstanceOf(
        ReadOnlyViolationError,
      );
      await expect(guardedFetch('http://api.test/api/v1/x', { method })).rejects.toMatchObject({
        name: 'ReadOnlyViolationError',
        method,
      });
    }

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('routes GET, HEAD, and OPTIONS through to fetch', async () => {
    const fetchMock = vi.fn(async () => new Response('', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    await guardedFetch('http://api.test/api/v1/x');
    await guardedFetch('http://api.test/api/v1/x', { method: 'HEAD' });
    await guardedFetch('http://api.test/api/v1/x', { method: 'OPTIONS' });

    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('guards against a mutation method carried by a Request object', async () => {
    await expect(
      guardedFetch(new Request('http://api.test/api/v1/x', { method: 'DELETE' })),
    ).rejects.toBeInstanceOf(ReadOnlyViolationError);
  });

  it('routes facade GETs through the guarded fetch', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({}));
    vi.stubGlobal('fetch', fetchMock);

    const client = createWebReadonlyClient({ baseUrl: 'http://api.test', fetch: guardedFetch });
    await client.getOverview();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const input = fetchMock.mock.calls[0]?.[0] as Request;
    const url = new URL(input.url);
    expect(url.origin).toBe('http://api.test');
    expect(url.pathname).toBe('/api/v1/overview');
  });
});
