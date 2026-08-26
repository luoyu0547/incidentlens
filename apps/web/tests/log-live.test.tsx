import { act, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { normalizeLogRouteSearch } from '../src/logs/log-search';
import { useLiveLogs } from '../src/logs/useLiveLogs';

class FakeSocket {
  static latest: FakeSocket | undefined;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  constructor() { FakeSocket.latest = this; }
  open() { this.onopen?.(); }
  send(value: string) { this.sent.push(value); }
  close() { this.onclose?.(); }
  emit(value: unknown) { this.onmessage?.({ data: JSON.stringify(value) }); }
}

function Probe() {
  const result = useLiveLogs('svc-web', normalizeLogRouteSearch({ mode: 'live' }), { webSocketFactory: (url) => { void url; return new FakeSocket() as unknown as WebSocket; } });
  return <div><output data-testid="status">{result.status}</output><output data-testid="records">{result.records.map(x => x.log_id).join(',')}</output><button onClick={result.pause}>pause</button></div>;
}

describe('useLiveLogs', () => {
  it('backfills before connecting, enters live only after subscribed, and appends records', async () => {
    render(<Probe />);
    expect(await screen.findByTestId('records')).toHaveTextContent('log-1');
    FakeSocket.latest!.open();
    await screen.findByText('live');
    expect(JSON.parse(FakeSocket.latest!.sent[0])).toMatchObject({ action: 'subscribe', service_id: 'svc-web', cursor: 'cursor-1' });
    await act(async () => FakeSocket.latest!.emit({ schema_version: 1, event_type: 'log.record', occurred_at: '2026-08-26T00:00:01Z', cursor: 'cursor-2', payload: { log_id: 'log-2', cursor: 'cursor-2', message: 'new', occurred_at: '2026-08-26T00:00:01Z', severity: 'info' } }));
    expect(screen.getByTestId('records')).toHaveTextContent('log-1,log-2');
    expect(FakeSocket.latest!.sent.some(value => JSON.parse(value).action === 'ack')).toBe(true);
  });
  it('ignores unknown events and sends pause without changing server state', async () => {
    render(<Probe />);
    await screen.findByText('live');
    await act(async () => FakeSocket.latest!.emit({ schema_version: 1, event_type: 'future.event', occurred_at: '2026-08-26T00:00:01Z' }));
    expect(screen.getByTestId('records')).toHaveTextContent('log-1');
    await act(async () => screen.getByRole('button', { name: 'pause' }).click());
    expect(FakeSocket.latest!.sent.some(value => JSON.parse(value).action === 'pause')).toBe(true);
  });
});
