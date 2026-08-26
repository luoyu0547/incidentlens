/**
 * Operation tracker tests.
 *
 * Verifies generic durable-operation following:
 * terminal state detection, safe redacted projection, network-failure
 * surfacing, AbortSignal forwarding, and no-leak of raw/canonical
 * payloads even if a server returns them.
 */

import { describe, expect, it, vi } from 'vitest';
import type { OperationView } from '@incidentlens/protocol';
import { trackOperation } from './operation-tracker.js';

function makeOperation(overrides: Partial<OperationView> = {}): OperationView {
  return {
    operation_id: 'op-1',
    kind: 'agent_message',
    target_id: 'target-1',
    session_id: 'session-1',
    investigation_id: null,
    status: 'running',
    progress_summary: null,
    error_code: null,
    error_message: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    finished_at: null,
    ...overrides,
  };
}

describe('trackOperation', () => {
  it('reports a succeeded operation with its progress summary', async () => {
    const succeeded = makeOperation({
      status: 'succeeded',
      progress_summary: 'Investigation complete',
    });
    const onResult = vi.fn();

    const result = await trackOperation(vi.fn().mockResolvedValue(succeeded), 'op-1', onResult, {
      pollIntervalMs: 1,
    });

    expect(result.status).toBe('succeeded');
    expect(result.summary).toContain('Investigation complete');
    expect(onResult).toHaveBeenCalledWith(result);
  });

  it('reports a failed operation with a safe error, never raw payloads', async () => {
    const failed = makeOperation({ status: 'failed', error_message: 'Agent paused (safe)' });
    const onResult = vi.fn();

    const result = await trackOperation(vi.fn().mockResolvedValue(failed), 'op-1', onResult, {
      pollIntervalMs: 1,
    });

    expect(result.status).toBe('failed');
    expect(result.error).toContain('safe');
  });

  it('reports uncertain when the operation never reaches a terminal state', async () => {
    const running = makeOperation({ status: 'running' });
    const onResult = vi.fn();

    const result = await trackOperation(vi.fn().mockResolvedValue(running), 'op-1', onResult, {
      pollIntervalMs: 1,
      maxPolls: 2,
    });

    expect(result.status).toBe('uncertain');
    expect(onResult).toHaveBeenCalledWith(result);
  });

  it('surfaces a getOperation network failure as a safe error', async () => {
    const onResult = vi.fn();
    const onError = vi.fn();

    const result = await trackOperation(
      vi.fn().mockRejectedValue(new Error('upstream unavailable')),
      'op-1',
      onResult,
      { pollIntervalMs: 1, maxPolls: 3, onError },
    );

    expect(result.status).toBe('failed');
    expect(result.error).toContain('upstream unavailable');
    expect(onError).toHaveBeenCalledWith(expect.stringContaining('upstream unavailable'));
    expect(onResult).toHaveBeenCalledWith(result);
  });

  it('passes the AbortSignal through to getOperation', async () => {
    const getOperation = vi.fn().mockResolvedValue(makeOperation({ status: 'succeeded' }));
    const controllerAbort = new AbortController();

    await trackOperation(getOperation, 'op-1', vi.fn(), {
      pollIntervalMs: 1,
      signal: controllerAbort.signal,
    });

    expect(getOperation).toHaveBeenCalledWith('op-1', controllerAbort.signal);
  });

  it('does not surface canonical intents, provider payloads, or tool raw args', async () => {
    // A malicious/buggy server returning raw fields must not leak them.
    const polluted = makeOperation({
      status: 'succeeded',
      progress_summary: 'safe summary',
    }) as OperationView & Record<string, unknown>;
    polluted['canonical_intent'] = 'SECRET_INTENT';
    polluted['request_payload'] = { tool: 'bash', args: 'rm -rf', output: 'TOP_SECRET' };

    const result = await trackOperation(vi.fn().mockResolvedValue(polluted), 'op-1', vi.fn(), {
      pollIntervalMs: 1,
    });

    // Only redacted fields are projected into the progress shape.
    expect(result).not.toHaveProperty('canonical_intent');
    expect(result).not.toHaveProperty('request_payload');
    expect(JSON.stringify(result)).not.toContain('SECRET_INTENT');
    expect(JSON.stringify(result)).not.toContain('TOP_SECRET');
    // Still accurate.
    expect(result.summary).toBe('safe summary');
  });
});