import { describe, expect, it } from 'vitest';
import { logSearchError, logSearchToQuery, normalizeLogRouteSearch } from '../src/logs/log-search';

describe('log search normalization', () => {
  it('uses safe defaults and removes empty values', () => {
    expect(normalizeLogRouteSearch({ q: '  ', context: 999 })).toMatchObject({ mode: 'history', levels: [], context: 100, follow: true });
  });
  it('accepts severity arrays and validates ISO times', () => {
    expect(normalizeLogRouteSearch({ levels: ['error', 'error', 'bogus'], from: '2026-01-01T00:00:00Z' })).toMatchObject({ levels: ['error'], from: '2026-01-01T00:00:00Z' });
    expect(normalizeLogRouteSearch({ from: 'tomorrow' }).from).toBeUndefined();
  });
  it('reports reversed times without making a request', () => {
    const search = normalizeLogRouteSearch({ from: '2026-01-02T00:00:00Z', to: '2026-01-01T00:00:00Z' });
    expect(logSearchError(search)).toBe('开始时间必须早于结束时间');
  });
  it('passes opaque cursors verbatim', () => {
    const cursor = 'lc1_docker:abc+/==:42';
    expect(logSearchToQuery(normalizeLogRouteSearch({}), cursor).before).toBe(cursor);
  });
});
