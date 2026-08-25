import { describe, it, expect } from 'vitest';
import {
  parseStreamFrame,
  assertCompatible,
  ProtocolError,
} from '../src/stream.js';
import type {
  CliStreamEnvelopeBase,
  ApiVersionView,
  ClientCompatibility,
} from '../src/stream.js';

// ---------------------------------------------------------------------------
// Step 1: Contract-readiness tests
// ---------------------------------------------------------------------------

describe('OpenAPI contract readiness', () => {
  it('exports expected stable operation IDs from generated SDK', async () => {
    const sdk = await import('../src/generated/sdk.gen.js');

    const requiredOps = [
      'getApiVersion',
      'getCurrentPrincipal',
      'listTargets',
      'getTarget',
      'testTarget',
      'listAgentSessions',
      'getAgentSession',
      'sendAgentMessage',
      'listAgentMessages',
      'getOperation',
      'listApprovals',
      'getApproval',
      'approveApproval',
      'rejectApproval',
      'listProductEvents',
    ];

    for (const op of requiredOps) {
      expect(sdk, `SDK should export ${op}`).toHaveProperty(op);
      expect(typeof (sdk as Record<string, unknown>)[op]).toBe('function');
    }
  });

  it('exports expected types from generated types', async () => {
    // All exports in types.gen.ts are TypeScript type-only exports (erased at
    // runtime), so we verify source-level presence by reading the file.
    const { readFileSync } = await import('node:fs');
    const { fileURLToPath } = await import('node:url');
    const { dirname, join } = await import('node:path');
    const typesPath = join(
      dirname(fileURLToPath(import.meta.url)),
      '../src/generated/types.gen.ts',
    );
    const source = readFileSync(typesPath, 'utf-8');

    const requiredTypes = [
      'ApiVersionView',
      'Principal',
      'TargetView',
      'AgentSessionView',
      'OperationView',
      'ApprovalDetailView',
      'EventPage',
      'StreamEventEnvelope',
      'RuntimeEventType',
      'HealthStatus',
    ];

    for (const t of requiredTypes) {
      expect(source, `types.gen.ts should export ${t}`).toContain(`export type ${t}`);
    }
  });

  it('exports schemas from generated schemas', async () => {
    const schemas = await import('../src/generated/schemas.gen.js');
    expect(schemas).toBeDefined();
    // schemas.gen.ts should have at least the validation objects
    const keys = Object.keys(schemas);
    expect(keys.length).toBeGreaterThan(0);
  });
});

describe('Stream schema discriminates by event_type', () => {
  it('cli-stream: stream.hello is a valid control event', () => {
    const frame = JSON.stringify({
      schema_version: 1,
      event_type: 'stream.hello',
      occurred_at: '2026-01-01T00:00:00Z',
      sequence: null,
    });
    const result = parseStreamFrame(frame);
    expect(result.kind).toBe('known');
    expect(result.envelope.event_type).toBe('stream.hello');
  });

  it('cli-stream: stream.heartbeat is a valid control event', () => {
    const frame = JSON.stringify({
      schema_version: 1,
      event_type: 'stream.heartbeat',
      occurred_at: '2026-01-01T00:00:00Z',
    });
    const result = parseStreamFrame(frame);
    expect(result.kind).toBe('known');
  });

  it('cli-stream: stream.gap is a valid control event', () => {
    const frame = JSON.stringify({
      schema_version: 1,
      event_type: 'stream.gap',
      occurred_at: '2026-01-01T00:00:00Z',
      sequence: 42,
    });
    const result = parseStreamFrame(frame);
    expect(result.kind).toBe('known');
  });

  it('cli-stream: stream.slow_consumer is a valid control event', () => {
    const frame = JSON.stringify({
      schema_version: 1,
      event_type: 'stream.slow_consumer',
      occurred_at: '2026-01-01T00:00:00Z',
    });
    const result = parseStreamFrame(frame);
    expect(result.kind).toBe('known');
  });

  it('cli-stream: operation.running is a valid runtime event', () => {
    const frame = JSON.stringify({
      schema_version: 1,
      event_type: 'operation.running',
      occurred_at: '2026-01-01T00:00:00Z',
      sequence: 7,
      payload: { operation_id: 'op-123' },
    });
    const result = parseStreamFrame(frame);
    expect(result.kind).toBe('known');
  });

  it('cli-stream: approval.requested is a valid runtime event', () => {
    const frame = JSON.stringify({
      schema_version: 1,
      event_type: 'approval.requested',
      occurred_at: '2026-01-01T00:00:00Z',
      investigation_id: 'inv-1',
    });
    const result = parseStreamFrame(frame);
    expect(result.kind).toBe('known');
  });

  it('cli-stream: agent.text.delta is a valid runtime event', () => {
    const frame = JSON.stringify({
      schema_version: 1,
      event_type: 'agent.text.delta',
      occurred_at: '2026-01-01T00:00:00Z',
      session_id: 'sess-1',
      payload: { chunk: 'hello' },
    });
    const result = parseStreamFrame(frame);
    expect(result.kind).toBe('known');
  });

  it('cli-stream: unknown event_type returns kind=unknown with base fields', () => {
    const frame = JSON.stringify({
      schema_version: 1,
      event_type: 'future_event.v2',
      occurred_at: '2026-01-01T00:00:00Z',
      sequence: 99,
      event_id: 'evt-x',
    });
    const result = parseStreamFrame(frame);
    expect(result.kind).toBe('unknown');
    expect(result.envelope.schema_version).toBe(1);
    expect(result.envelope.event_type).toBe('future_event.v2');
    expect(result.envelope.sequence).toBe(99);
    expect(result.envelope.event_id).toBe('evt-x');
  });
});

// ---------------------------------------------------------------------------
// Step 4: Strict stream parsing
// ---------------------------------------------------------------------------

describe('parseStreamFrame – error cases', () => {
  it('throws MALFORMED_JSON for non-JSON input', () => {
    try {
      parseStreamFrame('not json');
      expect.fail('should have thrown');
    } catch (e) {
      expect(e).toBeInstanceOf(ProtocolError);
      expect((e as ProtocolError).code).toBe('MALFORMED_JSON');
    }
  });

  it('throws NOT_OBJECT for JSON array', () => {
    try {
      parseStreamFrame('[]');
      expect.fail('should have thrown');
    } catch (e) {
      expect(e).toBeInstanceOf(ProtocolError);
      expect((e as ProtocolError).code).toBe('NOT_OBJECT');
    }
  });

  it('throws NOT_OBJECT for JSON primitive', () => {
    for (const input of ['"hello"', '42', 'null']) {
      try {
        parseStreamFrame(input);
        expect.fail('should have thrown');
      } catch (e) {
        expect(e).toBeInstanceOf(ProtocolError);
        expect((e as ProtocolError).code).toBe('NOT_OBJECT');
      }
    }
  });

  it('throws UNSUPPORTED_SCHEMA_VERSION for schema_version !== 1', () => {
    const frame = JSON.stringify({
      schema_version: 2,
      event_type: 'stream.hello',
      occurred_at: '2026-01-01T00:00:00Z',
    });
    try {
      parseStreamFrame(frame);
      expect.fail('should have thrown');
    } catch (e) {
      expect(e).toBeInstanceOf(ProtocolError);
      expect((e as ProtocolError).code).toBe('UNSUPPORTED_SCHEMA_VERSION');
    }
  });

  it('throws MISSING_EVENT_TYPE when event_type is absent', () => {
    const frame = JSON.stringify({
      schema_version: 1,
      occurred_at: '2026-01-01T00:00:00Z',
    });
    try {
      parseStreamFrame(frame);
      expect.fail('should have thrown');
    } catch (e) {
      expect(e).toBeInstanceOf(ProtocolError);
      expect((e as ProtocolError).code).toBe('MISSING_EVENT_TYPE');
    }
  });

  it('throws MISSING_EVENT_TYPE when event_type is empty string', () => {
    const frame = JSON.stringify({
      schema_version: 1,
      event_type: '',
      occurred_at: '2026-01-01T00:00:00Z',
    });
    try {
      parseStreamFrame(frame);
      expect.fail('should have thrown');
    } catch (e) {
      expect(e).toBeInstanceOf(ProtocolError);
      expect((e as ProtocolError).code).toBe('MISSING_EVENT_TYPE');
    }
  });

  it('throws MISSING_OCCURRED_AT when occurred_at is absent', () => {
    const frame = JSON.stringify({
      schema_version: 1,
      event_type: 'stream.hello',
    });
    try {
      parseStreamFrame(frame);
      expect.fail('should have thrown');
    } catch (e) {
      expect(e).toBeInstanceOf(ProtocolError);
      expect((e as ProtocolError).code).toBe('MISSING_OCCURRED_AT');
    }
  });
});

describe('parseStreamFrame – envelope fields', () => {
  it('preserves sequence, event_id, investigation_id, session_id, target_id', () => {
    const frame = JSON.stringify({
      schema_version: 1,
      event_type: 'operation.succeeded',
      occurred_at: '2026-08-25T12:00:00Z',
      sequence: 55,
      event_id: 'evt-abc',
      investigation_id: 'inv-1',
      session_id: 'sess-2',
      target_id: 'tgt-3',
      payload: { result: 'ok' },
    });
    const result = parseStreamFrame(frame);
    expect(result.kind).toBe('known');
    expect(result.envelope.sequence).toBe(55);
    expect(result.envelope.event_id).toBe('evt-abc');
    expect(result.envelope.investigation_id).toBe('inv-1');
    expect(result.envelope.session_id).toBe('sess-2');
    expect(result.envelope.target_id).toBe('tgt-3');
    expect(result.envelope.payload).toEqual({ result: 'ok' });
  });

  it('defaults optional fields to null when absent', () => {
    const frame = JSON.stringify({
      schema_version: 1,
      event_type: 'stream.heartbeat',
      occurred_at: '2026-08-25T12:00:00Z',
    });
    const result = parseStreamFrame(frame);
    expect(result.envelope.sequence).toBeNull();
    expect(result.envelope.event_id).toBeNull();
    expect(result.envelope.investigation_id).toBeNull();
    expect(result.envelope.session_id).toBeNull();
    expect(result.envelope.target_id).toBeNull();
    expect(result.envelope.payload).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// assertCompatible
// ---------------------------------------------------------------------------

describe('assertCompatible', () => {
  const client: ClientCompatibility = {
    min_protocol_version: '1.0.0',
    max_protocol_version: '1.9.0',
  };

  it('passes when server version is within range', () => {
    const server: ApiVersionView = { protocol_version: '1.5.0' };
    expect(() => assertCompatible(server, client)).not.toThrow();
  });

  it('passes at lower boundary', () => {
    const server: ApiVersionView = { protocol_version: '1.0.0' };
    expect(() => assertCompatible(server, client)).not.toThrow();
  });

  it('passes at upper boundary', () => {
    const server: ApiVersionView = { protocol_version: '1.9.0' };
    expect(() => assertCompatible(server, client)).not.toThrow();
  });

  it('throws VERSION_TOO_OLD when server is below min', () => {
    const server: ApiVersionView = { protocol_version: '0.9.0' };
    try {
      assertCompatible(server, client);
      expect.fail('should have thrown');
    } catch (e) {
      expect(e).toBeInstanceOf(ProtocolError);
      expect((e as ProtocolError).code).toBe('VERSION_TOO_OLD');
    }
  });

  it('throws VERSION_TOO_NEW when server is above max', () => {
    const server: ApiVersionView = { protocol_version: '2.0.0' };
    try {
      assertCompatible(server, client);
      expect.fail('should have thrown');
    } catch (e) {
      expect(e).toBeInstanceOf(ProtocolError);
      expect((e as ProtocolError).code).toBe('VERSION_TOO_NEW');
    }
  });

  it('throws MISSING_PROTOCOL_VERSION when server version is absent', () => {
    const server: ApiVersionView = {};
    try {
      assertCompatible(server, client);
      expect.fail('should have thrown');
    } catch (e) {
      expect(e).toBeInstanceOf(ProtocolError);
      expect((e as ProtocolError).code).toBe('MISSING_PROTOCOL_VERSION');
    }
  });
});
