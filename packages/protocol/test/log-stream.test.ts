import { describe, expect, it } from 'vitest';
import {
  parseLogStreamCommand, parseLogStreamEvent, serializeLogAck, serializeLogPause,
  serializeLogResume, serializeLogSubscribe, serializeLogUpdate,
} from '../src/log-stream';

describe('log stream protocol', () => {
  it('serializes the complete client action vocabulary', () => {
    expect(serializeLogSubscribe({ service_id: 'svc', cursor: 'c0' })).toEqual({ action: 'subscribe', service_id: 'svc', cursor: 'c0' });
    expect(serializeLogUpdate({ severity: 'error', cursor: 'c1' })).toEqual({ action: 'update', severity: 'error', cursor: 'c1' });
    expect(serializeLogPause()).toEqual({ action: 'pause' });
    expect(serializeLogResume('c1')).toEqual({ action: 'resume', cursor: 'c1' });
    expect(serializeLogAck('c2')).toEqual({ action: 'ack', cursor: 'c2' });
    expect(parseLogStreamCommand({ action: 'pause' })).toEqual({ action: 'pause' });
  });
  it('parses known events, ignores unknown valid events, and rejects malformed frames', () => {
    const base = { schema_version: 1, occurred_at: '2026-01-01T00:00:00.000Z' };
    expect(parseLogStreamEvent({ ...base, event_type: 'stream.heartbeat', cursor: 'c' })).toMatchObject({ event_type: 'stream.heartbeat' });
    expect(parseLogStreamEvent({ ...base, event_type: 'future.event', payload: { x: 1 } })).toEqual({ kind: 'unknown', event: expect.objectContaining({ event_type: 'future.event' }) });
    expect(() => parseLogStreamEvent({ ...base, event_type: 'log.record', extra: true })).toThrow();
    expect(() => parseLogStreamEvent({ schema_version: 2, event_type: 'log.record', occurred_at: base.occurred_at })).toThrow();
  });
});
