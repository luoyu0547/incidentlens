import { useCallback, useMemo, useRef, useState } from 'react';
import type { LogRecordView } from '@incidentlens/protocol';

export interface LogLocator {
  readonly service: string;
  readonly cursor?: string | null;
  readonly log_id?: string | null;
  readonly context?: number | null;
  readonly evidence?: string | null;
  readonly issue?: string | null;
}

export interface LogAnchorRecord extends LogRecordView {
  readonly highlighted?: boolean;
}

export interface UseLogAnchorOptions {
  readonly locator?: LogLocator | null;
  readonly currentService?: string;
  readonly records?: readonly LogRecordView[];
  readonly fetchContext?: (locator: LogLocator) => Promise<readonly LogRecordView[]>;
  readonly onRecords?: (records: readonly LogRecordView[]) => void;
  readonly onExpired?: (locator: LogLocator) => void;
  readonly scrollTo?: (logId: string) => void;
}

export function logLocatorUrl(locator: LogLocator): string {
  const params = new URLSearchParams();
  // Keep both opaque server identifiers. Neither is derived from display data.
  if (locator.log_id) params.set('anchor', locator.log_id);
  if (locator.cursor) params.set('cursor', locator.cursor);
  if (locator.evidence) params.set('evidence', locator.evidence);
  if (locator.issue) params.set('issue', locator.issue);
  if (locator.context != null) params.set('context', String(locator.context));
  const query = params.toString();
  return `/services/${encodeURIComponent(locator.service)}${query ? `?${query}` : ''}`;
}

export function evidenceSummaryUrl(locator: LogLocator): string {
  if (locator.issue) {
    const params = locator.evidence ? `?evidence=${encodeURIComponent(locator.evidence)}` : '';
    return `/issues/${encodeURIComponent(locator.issue)}${params}`;
  }
  return locator.evidence ? `/issues?evidence=${encodeURIComponent(locator.evidence)}` : '/issues';
}

function isExpired(error: unknown): boolean {
  if (typeof error === 'object' && error !== null && 'status' in error) {
    const status = (error as { status?: unknown }).status;
    return status === 400 || status === 404 || status === 410;
  }
  return error instanceof Error && /expired|cursor|not found/i.test(error.message);
}

function mergeRecords(records: readonly LogRecordView[], additions: readonly LogRecordView[]): LogRecordView[] {
  const merged = new Map<string, LogRecordView>();
  for (const record of [...records, ...additions]) merged.set(record.log_id, record);
  return [...merged.values()];
}

export function useLogAnchor(options: UseLogAnchorOptions) {
  const { locator, currentService, records = [], fetchContext, onRecords, onExpired, scrollTo } = options;
  const [loading, setLoading] = useState(false);
  const [expired, setExpired] = useState(false);
  const anchorId = locator?.log_id ?? undefined;
  const present = Boolean(anchorId && records.some((record) => record.log_id === anchorId));
  const url = useMemo(() => locator ? logLocatorUrl(locator) : undefined, [locator]);

  const locating = useRef(false);
  const locate = useCallback(async () => {
    if (locating.current || !locator || !anchorId) return;
    locating.current = true;
    try {
      // A service mismatch must be handled before any query/context request.
      if (currentService !== locator.service) {
        window.location.assign(url ?? logLocatorUrl(locator));
        return;
      }
      if (records.some((record) => record.log_id === anchorId)) {
        requestAnimationFrame(() => scrollTo?.(anchorId));
        return;
      }
      if (!fetchContext) return;
      setLoading(true);
      setExpired(false);
      const context = await fetchContext(locator);
      const merged = mergeRecords(records, context);
      onRecords?.(merged);
      if (merged.some((record) => record.log_id === anchorId)) {
        requestAnimationFrame(() => scrollTo?.(anchorId));
      }
    } catch (error) {
      if (isExpired(error)) {
        setExpired(true);
        onExpired?.(locator);
      } else throw error;
    } finally {
      setLoading(false);
      locating.current = false;
    }
  }, [locator, anchorId, currentService, url, records, fetchContext, onRecords, onExpired, scrollTo]);

  return { anchorId, url, present, loading, expired, locate };
}

export { mergeRecords };
