import type { LogRecordView } from '@incidentlens/protocol';

export type LogBodyPresentation =
  | { kind: 'json'; value: unknown; summary: string }
  | { kind: 'stack'; headline: string; lines: readonly string[] }
  | { kind: 'text'; text: string };

const JSON_KEYS = ['structured_json', 'json', 'json_data', 'structured'] as const;
const STACK_KEYS = ['stack_trace', 'stacktrace', 'stack'] as const;

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function firstField(fields: Record<string, unknown> | undefined, keys: readonly string[]): unknown {
  if (!fields) return undefined;
  for (const key of keys) if (key in fields && fields[key] !== undefined && fields[key] !== null) return fields[key];
  return undefined;
}

function jsonSummary(value: unknown): string {
  if (Array.isArray(value)) return `${value.length} 项`;
  if (asRecord(value)) return `${Object.keys(value as Record<string, unknown>).length} 个字段`;
  return typeof value;
}

function stackValue(value: unknown, message: string): { headline: string; lines: readonly string[] } | undefined {
  if (Array.isArray(value) && value.every((line) => typeof line === 'string')) {
    return { headline: message.split(/\r?\n/, 1)[0] ?? message, lines: value };
  }
  if (typeof value === 'string') {
    const parts = value.split(/\r?\n/);
    return { headline: parts.shift() ?? '', lines: parts };
  }
  const object = asRecord(value);
  if (!object) return undefined;
  const lines = object.lines;
  if (!Array.isArray(lines) || !lines.every((line) => typeof line === 'string')) return undefined;
  return { headline: typeof object.headline === 'string' ? object.headline : message.split(/\r?\n/, 1)[0] ?? message, lines };
}

/** Presents only the server-provided redacted message and structured fields. */
export function presentLogBody(record: LogRecordView): LogBodyPresentation {
  const fields = asRecord(record.fields);
  const stack = stackValue(firstField(fields, STACK_KEYS), record.message);
  if (stack) return { kind: 'stack', ...stack };

  let value = firstField(fields, JSON_KEYS);
  if (value === undefined) {
    try { value = JSON.parse(record.message) as unknown; } catch { return { kind: 'text', text: record.message }; }
  } else if (typeof value === 'string') {
    try { value = JSON.parse(value) as unknown; } catch { return { kind: 'text', text: record.message }; }
  }
  return { kind: 'json', value, summary: `${record.message} · ${jsonSummary(value)}` };
}

export function presentationText(presentation: LogBodyPresentation): string {
  if (presentation.kind === 'text') return presentation.text;
  if (presentation.kind === 'stack') return [presentation.headline, ...presentation.lines].join('\n');
  return JSON.stringify(presentation.value, null, 2);
}
