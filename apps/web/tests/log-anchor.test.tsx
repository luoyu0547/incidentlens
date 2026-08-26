import { renderHook, act } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { evidenceSummaryUrl, logLocatorUrl, mergeRecords, useLogAnchor } from '../src/logs/useLogAnchor';
import { EvidenceMarker } from '../src/logs/EvidenceMarker';
import { LogLocatorLink } from '../src/issues/LogLocatorLink';
import { render, screen } from '@testing-library/react';
import type { LogRecordView } from '@incidentlens/protocol';

const record = (id: string, cursor = `cursor-${id}`): LogRecordView => ({ log_id: id, cursor, message: `display-${id}`, occurred_at: 'not-used', severity: 'info' });
const locator = { service: 'payments/api', cursor: 'opaque-cursor', log_id: 'log-2', context: 30, evidence: 'ev-1', issue: 'issue-1' } as const;

describe('log locator', () => {
  it('builds a service URL only from structured locator fields', () => {
    expect(logLocatorUrl(locator)).toBe('/services/payments%2Fapi?anchor=log-2&cursor=opaque-cursor&evidence=ev-1&issue=issue-1&context=30');
    expect(evidenceSummaryUrl(locator)).toBe('/issues/issue-1?evidence=ev-1');
  });

  it('merges context and deduplicates by server log id', () => {
    expect(mergeRecords([record('1'), record('2')], [record('2', 'new'), record('3')]).map((x) => `${x.log_id}:${x.cursor}`)).toEqual(['1:cursor-1', '2:new', '3:cursor-3']);
  });

  it('fetches missing anchor, then centers the exact id', async () => {
    const onRecords = vi.fn();
    const scrollTo = vi.fn();
    const fetchContext = vi.fn(async () => [record('2')]);
    const { result } = renderHook(() => useLogAnchor({ locator, currentService: locator.service, records: [record('1')], fetchContext, onRecords, scrollTo }));
    await act(async () => { await result.current.locate(); });
    expect(fetchContext).toHaveBeenCalledWith(locator);
    expect(onRecords.mock.calls[0][0].map((x: LogRecordView) => x.log_id)).toEqual(['1', '2']);
    expect(scrollTo).toHaveBeenCalledWith('log-2');
  });

  it('navigates to the service before querying on mismatch', async () => {
    const assign = vi.spyOn(window.location, 'assign').mockImplementation(() => undefined);
    const fetchContext = vi.fn();
    const { result } = renderHook(() => useLogAnchor({ locator, currentService: 'other', fetchContext }));
    await act(async () => { await result.current.locate(); });
    expect(assign).toHaveBeenCalledWith(logLocatorUrl(locator));
    expect(fetchContext).not.toHaveBeenCalled();
    assign.mockRestore();
  });

  it('falls back when the opaque cursor expires', async () => {
    const onExpired = vi.fn();
    const { result } = renderHook(() => useLogAnchor({ locator, currentService: locator.service, fetchContext: async () => { throw { status: 410 }; }, onExpired }));
    await act(async () => { await result.current.locate(); });
    expect(result.current.expired).toBe(true);
    expect(onExpired).toHaveBeenCalledWith(locator);
  });

  it('renders marker and locator link with server ids', () => {
    render(<><EvidenceMarker record={record('log-2')} /><LogLocatorLink locator={locator}>证据日志</LogLocatorLink></>);
    expect(screen.getByRole('button', { name: '定位日志 log-2' })).toHaveAttribute('data-log-id', 'log-2');
    expect(screen.getByRole('link', { name: '证据日志' })).toHaveAttribute('href', logLocatorUrl(locator));
  });
});
