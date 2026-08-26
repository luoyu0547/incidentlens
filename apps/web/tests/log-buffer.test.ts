import type { LogRecordView } from '@incidentlens/protocol';
import { describe, expect, it } from 'vitest';
import { createLogBufferState, reduceLogBuffer } from '../src/logs/log-buffer';

const record = (id: string, cursor = id): LogRecordView => ({ log_id: id, cursor, message: id, occurred_at: id, severity: 'info' });

describe('bounded log buffer', () => {
  it('deduplicates by log id and preserves backend order', () => {
    let state = createLogBufferState();
    state = reduceLogBuffer(state, { type: 'appendMany', records: [record('b', '2'), record('a', '1')] });
    state = reduceLogBuffer(state, { type: 'append', record: record('b', 'new') });
    expect(state.records.map((item) => item.log_id)).toEqual(['b', 'a']);
  });
  it('evicts oldest records at the default bound', () => {
    const state = reduceLogBuffer(createLogBufferState(), { type: 'appendMany', records: Array.from({ length: 5_001 }, (_, i) => record(String(i))) });
    expect(state.records).toHaveLength(5_000);
    expect(state.records[0].log_id).toBe('1');
    expect(state.droppedBeforeCount).toBe(1);
  });
  it('tracks paused unread records and clears unread on resume', () => {
    let state = reduceLogBuffer(createLogBufferState(), { type: 'setPaused', paused: true });
    state = reduceLogBuffer(state, { type: 'appendMany', records: [record('1'), record('2')] });
    expect(state.unreadCount).toBe(2);
    state = reduceLogBuffer(state, { type: 'resume' });
    expect(state.unreadCount).toBe(0);
    expect(state.records).toHaveLength(2);
  });
});
