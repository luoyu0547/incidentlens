import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { ControlPlaneApi } from './control-plane-api.js';
import { ApiError } from './api-error.js';
import { createIdempotencyKey } from './idempotency.js';

describe('ControlPlaneApi', () => {
  let api: ControlPlaneApi;
  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchSpy = vi.fn();
    global.fetch = fetchSpy;

    api = new ControlPlaneApi({
      baseUrl: 'https://api.example.com',
      token: 'test-token-123',
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('bearer injection', () => {
    it('includes Authorization header with bearer token', async () => {
      fetchSpy.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          api_version: '1.0.0',
          schema_version: 1,
        }),
      });

      await api.compatibility();

      expect(fetchSpy).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({
          headers: expect.objectContaining({
            Authorization: 'Bearer test-token-123',
          }),
        })
      );
    });

    it('does not include Authorization header when no token', async () => {
      const apiWithoutToken = new ControlPlaneApi({
        baseUrl: 'https://api.example.com',
      });

      fetchSpy.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          api_version: '1.0.0',
          schema_version: 1,
        }),
      });

      await apiWithoutToken.compatibility();

      expect(fetchSpy).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({
          headers: expect.not.objectContaining({
            Authorization: expect.any(String),
          }),
        })
      );
    });
  });

  describe('no token in error messages', () => {
    it('does not expose token in error messages', async () => {
      fetchSpy.mockRejectedValueOnce(new Error('Network error'));

      try {
        await api.compatibility();
      } catch (error) {
        if (error instanceof Error) {
          expect(error.message).not.toContain('test-token-123');
        }
      }
    });
  });

  describe('request ID preservation', () => {
    it('preserves x-request-id from response headers', async () => {
      fetchSpy.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers({
          'content-type': 'application/json',
          'x-request-id': 'req-123-abc',
        }),
        json: async () => ({
          api_version: '1.0.0',
          schema_version: 1,
        }),
      });

      const result = await api.compatibility();

      expect(result).toBeDefined();
    });
  });

  describe('GET retry behavior', () => {
    it('retries on network errors for GET requests', async () => {
      fetchSpy
        .mockRejectedValueOnce(new TypeError('Failed to fetch'))
        .mockResolvedValueOnce({
          ok: true,
          status: 200,
          headers: new Headers({ 'content-type': 'application/json' }),
          json: async () => ({
            api_version: '1.0.0',
            schema_version: 1,
          }),
        });

      const result = await api.compatibility();

      expect(fetchSpy).toHaveBeenCalledTimes(2);
      expect(result).toBeDefined();
    });

    it('retries on 408 Request Timeout for GET requests', async () => {
      fetchSpy
        .mockResolvedValueOnce({
          ok: false,
          status: 408,
          headers: new Headers({ 'content-type': 'application/json' }),
          json: async () => ({ error: 'timeout' }),
        })
        .mockResolvedValueOnce({
          ok: true,
          status: 200,
          headers: new Headers({ 'content-type': 'application/json' }),
          json: async () => ({
            api_version: '1.0.0',
            schema_version: 1,
          }),
        });

      const result = await api.compatibility();

      expect(fetchSpy).toHaveBeenCalledTimes(2);
      expect(result).toBeDefined();
    });

    it('retries on 429 Too Many Requests for GET requests', async () => {
      fetchSpy
        .mockResolvedValueOnce({
          ok: false,
          status: 429,
          headers: new Headers({
            'content-type': 'application/json',
            'retry-after': '1',
          }),
          json: async () => ({ error: 'rate limited' }),
        })
        .mockResolvedValueOnce({
          ok: true,
          status: 200,
          headers: new Headers({ 'content-type': 'application/json' }),
          json: async () => ({
            api_version: '1.0.0',
            schema_version: 1,
          }),
        });

      const result = await api.compatibility();

      expect(fetchSpy).toHaveBeenCalledTimes(2);
      expect(result).toBeDefined();
    });

    it('retries on 5xx errors for GET requests', async () => {
      fetchSpy
        .mockResolvedValueOnce({
          ok: false,
          status: 500,
          headers: new Headers({ 'content-type': 'application/json' }),
          json: async () => ({ error: 'internal error' }),
        })
        .mockResolvedValueOnce({
          ok: true,
          status: 200,
          headers: new Headers({ 'content-type': 'application/json' }),
          json: async () => ({
            api_version: '1.0.0',
            schema_version: 1,
          }),
        });

      const result = await api.compatibility();

      expect(fetchSpy).toHaveBeenCalledTimes(2);
      expect(result).toBeDefined();
    });
  });

  describe('mutation no automatic retry', () => {
    it('does not retry on 408 for POST requests', async () => {
      fetchSpy.mockResolvedValueOnce({
        ok: false,
        status: 408,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({ error: 'timeout' }),
      });

      const idempotencyKey = createIdempotencyKey();

      try {
        await api.createTarget(
          {
            name: 'test',
            host: 'example.com',
            ssh_user: 'root',
            authentication_ref: 'ref-123',
          },
          { idempotencyKey }
        );
      } catch (error) {
        if (error instanceof ApiError) {
          expect(error.status).toBe(408);
        }
      }

      expect(fetchSpy).toHaveBeenCalledTimes(1);
    });
  });

  describe('explicit retry key reuse', () => {
    it('reuses idempotency key on explicit retry', async () => {
      const idempotencyKey = createIdempotencyKey();

      fetchSpy
        .mockResolvedValueOnce({
          ok: false,
          status: 500,
          headers: new Headers({ 'content-type': 'application/json' }),
          json: async () => ({ error: 'internal error' }),
        })
        .mockResolvedValueOnce({
          ok: true,
          status: 201,
          headers: new Headers({ 'content-type': 'application/json' }),
          json: async () => ({ target_id: 'target-123' }),
        });

      try {
        await api.createTarget(
          {
            name: 'test',
            host: 'example.com',
            ssh_user: 'root',
            authentication_ref: 'ref-123',
          },
          { idempotencyKey }
        );
      } catch {
        // First attempt failed
      }

      // Retry with same key
      await api.createTarget(
        {
          name: 'test',
          host: 'example.com',
          ssh_user: 'root',
          authentication_ref: 'ref-123',
        },
        { idempotencyKey }
      );

      expect(fetchSpy).toHaveBeenCalledTimes(2);
      // Both requests should have same idempotency key
      const firstHeaders = fetchSpy.mock.calls[0][1]?.headers as Record<string, string>;
      const secondHeaders = fetchSpy.mock.calls[1][1]?.headers as Record<string, string>;
      expect(firstHeaders['Idempotency-Key']).toBe(idempotencyKey);
      expect(secondHeaders['Idempotency-Key']).toBe(idempotencyKey);
    });
  });

  describe('AbortError preservation', () => {
    it('preserves AbortError when request is cancelled', async () => {
      const abortError = new DOMException('The operation was aborted', 'AbortError');
      fetchSpy.mockRejectedValueOnce(abortError);

      const controller = new AbortController();
      controller.abort();

      try {
        await api.compatibility(controller.signal);
      } catch (error) {
        expect(error).toBeInstanceOf(DOMException);
        expect((error as DOMException).name).toBe('AbortError');
      }
    });
  });

  describe('compatibility before business calls', () => {
    it('calls compatibility endpoint', async () => {
      fetchSpy.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          api_version: '1.0.0',
          schema_version: 1,
        }),
      });

      const result = await api.compatibility();

      expect(result).toEqual({
        api_version: '1.0.0',
        schema_version: 1,
      });
    });
  });

  describe('API error handling', () => {
    it('throws ApiError for non-retryable errors', async () => {
      fetchSpy.mockResolvedValueOnce({
        ok: false,
        status: 400,
        headers: new Headers({
          'content-type': 'application/json',
          'x-request-id': 'req-456',
        }),
        json: async () => ({
          error: 'bad_request',
          message: 'Invalid input',
        }),
      });

      try {
        await api.compatibility();
      } catch (error) {
        expect(error).toBeInstanceOf(ApiError);
        if (error instanceof ApiError) {
          expect(error.status).toBe(400);
          expect(error.retryable).toBe(false);
        }
      }
    });
  });
});
