import type { LogSeverity } from '@incidentlens/protocol';

export interface LogRouteSearch {
  readonly target?: string;
  readonly instance?: string;
  readonly levels: readonly LogSeverity[];
  readonly from?: string;
  readonly to?: string;
  readonly q?: string;
  readonly mode: 'history' | 'live';
  readonly anchor?: string;
  readonly evidence?: string;
  readonly issue?: string;
  readonly context: number;
  readonly follow: boolean;
}

const SEVERITIES: readonly LogSeverity[] = ['trace', 'debug', 'info', 'notice', 'warn', 'error', 'critical', 'unknown'];
const severitySet = new Set<string>(SEVERITIES);
const optionalText = (value: unknown): string | undefined => {
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
};
const iso = (value: unknown): string | undefined => {
  const text = optionalText(value);
  if (!text || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(text)) return undefined;
  return Number.isNaN(Date.parse(text)) ? undefined : text;
};
const values = (value: unknown): unknown[] => Array.isArray(value) ? value : typeof value === 'string' ? value.split(',') : [];

export function normalizeLogRouteSearch(input: unknown): LogRouteSearch {
  const source = typeof input === 'object' && input !== null ? input as Record<string, unknown> : {};
  const levels = [...new Set(values(source.levels).filter((x): x is LogSeverity => typeof x === 'string' && severitySet.has(x)))];
  const contextValue = typeof source.context === 'number' ? source.context : Number(source.context);
  const context = Number.isFinite(contextValue) ? Math.min(100, Math.max(1, Math.trunc(contextValue))) : 20;
  const mode = source.mode === 'live' ? 'live' : 'history';
  const from = iso(source.from);
  const to = iso(source.to);
  return {
    target: optionalText(source.target), instance: optionalText(source.instance), levels,
    from, to, q: optionalText(source.q), mode,
    anchor: optionalText(source.anchor), evidence: optionalText(source.evidence), issue: optionalText(source.issue),
    context, follow: source.follow === undefined ? true : source.follow === true || source.follow === 'true',
  };
}

export function logSearchToQuery(search: LogRouteSearch, cursor?: string): Record<string, string | number> {
  const query: Record<string, string | number> = { limit: search.context };
  if (search.instance) query.instance = search.instance;
  if (search.from) query.after = search.from;
  if (search.to) query.before = search.to;
  if (search.q) query.q = search.q;
  if (search.levels.length) query.severity = search.levels.join(',');
  if (search.anchor) query.anchor = search.anchor;
  if (cursor !== undefined) query.before = cursor;
  return query;
}

export function logSearchError(search: LogRouteSearch): string | undefined {
  if (search.from && search.to && Date.parse(search.from) > Date.parse(search.to)) return '开始时间必须早于结束时间';
  return undefined;
}
