import { describe, expect, it } from 'vitest';
import { presentLogBody, presentationText } from '../src/logs/log-presentation';
import { highlightSegments } from '../src/shared/highlight';
import { render, screen } from '@testing-library/react';
import { StructuredJson } from '../src/logs/StructuredJson';
import { StackTrace } from '../src/logs/StackTrace';
import type { LogRecordView } from '@incidentlens/protocol';

const record = (message: string, fields?: Record<string, unknown>) => ({ cursor: 'c', log_id: 'l', occurred_at: '2026-01-01T00:00:00Z', severity: 'error', message, fields }) as LogRecordView;

describe('safe log presentation', () => {
  it('keeps script markup as text and invalid JSON as text', () => {
    expect(presentLogBody(record('<script>alert(1)</script>')).kind).toBe('text');
    expect(presentLogBody(record('{invalid')).kind).toBe('text');
  });
  it('parses valid JSON and preserves redaction markers', () => {
    const result = presentLogBody(record('{"token":"[REDACTED]"}'));
    expect(result).toMatchObject({ kind: 'json', value: { token: '[REDACTED]' } });
    expect(presentationText(result)).toContain('[REDACTED]');
  });
  it('uses structured fields before message parsing', () => {
    expect(presentLogBody(record('not json', { structured_json: { nested: { safe: true } } }))).toMatchObject({ kind: 'json' });
  });
  it('renders nested JSON safely with collapse controls', () => {
    render(<StructuredJson value={{ nested: { value: '<script>' } }} />);
    expect(screen.getByText('<script>')).toBeInTheDocument();
    const buttons = screen.getAllByRole('button');
    expect(buttons.length).toBeGreaterThan(1);
    buttons[0]?.click();
    expect(screen.queryByText('<script>')).not.toBeInTheDocument();
  });
  it('preserves stack order and whitespace as text', () => {
    render(<StackTrace headline="Error: [REDACTED]" lines={['  at first', '', '  at second']} />);
    expect(screen.getByLabelText('stack trace').textContent).toBe('Error: [REDACTED]\n  at first\n\n  at second');
  });
  it('highlights literal queries without regex behavior', () => {
    expect(highlightSegments('a.b [x]', '.b')).toEqual([{ text: 'a', highlighted: false }, { text: '.b', highlighted: true }, { text: ' [x]', highlighted: false }]);
  });
  it('does not expose an HTML injection API', () => {
    expect(StructuredJson.toString()).not.toContain('dangerouslySetInnerHTML');
  });
});
