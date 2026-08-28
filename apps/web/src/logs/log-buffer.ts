import type { LogRecordView } from '@incidentlens/protocol';

export interface LogBufferState {
  readonly records: readonly LogRecordView[];
  readonly cursor: string | null;
  readonly droppedBeforeCount: number;
  readonly paused: boolean;
  readonly unreadCount: number;
}

export type LogBufferAction =
  | { readonly type: 'append'; readonly record: LogRecordView; readonly cursor?: string | null }
  | { readonly type: 'appendMany'; readonly records: readonly LogRecordView[]; readonly cursor?: string | null }
  | { readonly type: 'prepend'; readonly records: readonly LogRecordView[]; readonly cursor?: string | null }
  | { readonly type: 'setPaused'; readonly paused: boolean }
  | { readonly type: 'resume' }
  | { readonly type: 'markRead' }
  | { readonly type: 'clear' };

export const DEFAULT_MAX_RECORDS = 5_000;

export function createLogBufferState(): LogBufferState {
  return { records: [], cursor: null, droppedBeforeCount: 0, paused: false, unreadCount: 0 };
}

export function reduceLogBuffer(
  state: LogBufferState,
  action: LogBufferAction,
  maxRecords = DEFAULT_MAX_RECORDS,
): LogBufferState {
  const limit = Math.max(0, Math.floor(maxRecords));
  if (action.type === 'setPaused') {
    return { ...state, paused: action.paused, unreadCount: action.paused ? state.unreadCount : 0 };
  }
  if (action.type === 'resume' || action.type === 'markRead') return { ...state, paused: action.type === 'resume' ? false : state.paused, unreadCount: 0 };
  if (action.type === 'clear') return { ...createLogBufferState(), paused: state.paused };
  const incoming = action.type === 'append' ? [action.record] : action.records;
  const known = new Set(state.records.map((record) => record.log_id));
  const fresh = incoming.filter((record) => !known.has(record.log_id));
  if (fresh.length === 0) return { ...state, cursor: action.cursor === undefined ? state.cursor : action.cursor };
  const records = action.type === 'prepend' ? [...fresh, ...state.records] : [...state.records, ...fresh];
  const dropped = Math.max(0, records.length - limit);
  const bounded = dropped > 0 ? records.slice(dropped) : records;
  return {
    ...state,
    records: bounded,
    cursor: action.cursor === undefined ? fresh[fresh.length - 1]?.cursor ?? state.cursor : action.cursor,
    droppedBeforeCount: state.droppedBeforeCount + dropped,
    unreadCount: state.paused && action.type !== 'prepend' ? state.unreadCount + fresh.length : state.unreadCount,
  };
}
