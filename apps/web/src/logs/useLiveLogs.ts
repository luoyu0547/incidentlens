import { useCallback, useEffect, useRef, useState } from 'react';
import type { LogRecordView, ServiceLogQuery } from '@incidentlens/protocol';
import { parseLogStreamEvent, serializeLogAck, serializeLogPause, serializeLogResume, serializeLogSubscribe } from '@incidentlens/protocol';
import { readonlyClient } from '../api/client';
import { assertReadOnlyLogAction } from '../api/read-only-guard';
import { logSearchToQuery, type LogRouteSearch } from './log-search';

export interface UseLiveLogsResult {
  readonly records: readonly LogRecordView[];
  readonly status: 'connecting' | 'backfilling' | 'live' | 'paused' | 'reconnecting' | 'gap' | 'error';
  readonly unreadCount: number;
  readonly lastCursor: string | null;
  readonly error: Error | null;
  pause(): void;
  resume(): void;
  retry(): void;
}
export interface LiveLogsOptions { readonly webSocketFactory?: (url: string) => WebSocket; readonly maxRetries?: number; readonly backoffMs?: number; }
const MAX_RETRIES = 5;
const merge = (old: LogRecordView[], incoming: LogRecordView[]) => { const seen = new Set(old.map(x => x.log_id)); return [...old, ...incoming.filter(x => !seen.has(x.log_id))]; };

export function useLiveLogs(serviceId: string, search: LogRouteSearch, options: LiveLogsOptions = {}): UseLiveLogsResult {
  const [records, setRecords] = useState<LogRecordView[]>([]);
  const [status, setStatus] = useState<UseLiveLogsResult['status']>('connecting');
  const [lastCursor, setLastCursor] = useState<string | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [unreadCount, setUnreadCount] = useState(0);
  const socketRef = useRef<WebSocket | null>(null);
  const cursorRef = useRef<string | null>(null);
  const pausedRef = useRef(false);
  const retries = useRef(0);
  const generation = useRef(0);
  const maxRetries = options.maxRetries ?? MAX_RETRIES;
  const backoffMs = options.backoffMs ?? 250;

  const closeSocket = useCallback(() => { socketRef.current?.close(); socketRef.current = null; }, []);
  const query = useCallback((cursor?: string | null): ServiceLogQuery => logSearchToQuery(search, cursor ?? undefined) as ServiceLogQuery, [search]);
  const append = useCallback((record: LogRecordView) => { setRecords(prev => merge(prev, [record])); setUnreadCount(n => pausedRef.current ? n + 1 : 0); }, []);

  useEffect(() => {
    const runId = ++generation.current;
    let disposed = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const wsFactory = options.webSocketFactory ?? ((url: string) => new WebSocket(url));
    const initialQuery = query();
    const socketUrl = `${window.location.origin.replace(/^http/, 'ws')}/ws/v1/logs`;
    const backfill = async (authoritative = false) => {
      if (disposed || runId !== generation.current) return null;
      setStatus(authoritative ? 'gap' : 'backfilling');
      const fresh = await readonlyClient.getServiceLogs(serviceId, query(authoritative ? undefined : cursorRef.current));
      if (authoritative) setRecords(fresh.items);
      else setRecords(prev => merge(prev, fresh.items));
      let page = fresh;
      while (page.has_more && page.next_cursor) {
        page = await readonlyClient.getServiceLogs(serviceId, query(page.next_cursor));
        if (authoritative) setRecords(prev => merge(prev, page.items));
        else setRecords(prev => merge(prev, page.items));
      }
      const c = page.items.at(-1)?.cursor ?? page.snapshot_cursor;
      if (c) { cursorRef.current = c; setLastCursor(c); }
      return fresh;
    };
    const connect = async (initial = false) => {
      if (disposed) return;
      try {
        if (initial) {
          const page = await readonlyClient.getServiceLogs(serviceId, initialQuery);
          if (disposed) return;
          setRecords(page.items); const c = page.items.at(-1)?.cursor ?? page.snapshot_cursor;
          cursorRef.current = c; setLastCursor(c);
        } else await backfill();
        if (disposed) return;
        setStatus('connecting');
        const ws = wsFactory(socketUrl); socketRef.current = ws;
        const send = (payload: string) => {
          const command = JSON.parse(payload) as { action?: string };
          assertReadOnlyLogAction(command.action ?? '');
          ws.send(payload);
        };
        ws.onopen = () => { retries.current = 0; send(JSON.stringify(serializeLogSubscribe({ service_id: serviceId, target_id: search.target, severity: search.levels.length ? search.levels.join(',') : undefined, cursor: cursorRef.current }))); };
        ws.onmessage = (message) => {
          try {
            const parsed = parseLogStreamEvent(JSON.parse(String(message.data)));
            if ('kind' in parsed) return;
            if (parsed.event_type === 'log.record') { const record = parsed.payload as LogRecordView; append(record); if (parsed.cursor) { cursorRef.current = parsed.cursor; setLastCursor(parsed.cursor); send(JSON.stringify(serializeLogAck(parsed.cursor))); } }
            else if (parsed.event_type === 'log.subscribed') setStatus(pausedRef.current ? 'paused' : 'live');
            else if (parsed.event_type === 'stream.gap') { closeSocket(); void backfill(true).then(() => { if (!disposed) void connect(false); }); }
          } catch (e) { setError(e instanceof Error ? e : new Error('Invalid log stream event')); setStatus('error'); }
        };
        ws.onclose = () => { if (disposed) return; socketRef.current = null; if (retries.current >= maxRetries) { setError(new Error('Log stream retry limit exceeded')); setStatus('error'); return; } retries.current += 1; setStatus('reconnecting'); retryTimer = setTimeout(() => void connect(false), backoffMs * 2 ** (retries.current - 1)); };
        ws.onerror = () => { /* close drives bounded recovery */ };
      } catch (e) { setError(e instanceof Error ? e : new Error('Unable to connect to log stream')); setStatus('error'); }
    };
    void connect(true);
    return () => { disposed = true; if (retryTimer) clearTimeout(retryTimer); closeSocket(); };
  }, [serviceId, search, query, append, closeSocket, options.webSocketFactory, maxRetries, backoffMs]);

  const pause = useCallback(() => { pausedRef.current = true; const command = serializeLogPause(); assertReadOnlyLogAction(command.action); socketRef.current?.send(JSON.stringify(command)); setStatus('paused'); }, []);
  const resume = useCallback(() => { pausedRef.current = false; setUnreadCount(0); const command = serializeLogResume(cursorRef.current); assertReadOnlyLogAction(command.action); socketRef.current?.send(JSON.stringify(command)); setStatus('connecting'); }, []);
  const retry = useCallback(() => { retries.current = 0; setError(null); closeSocket(); }, [closeSocket]);
  return { records, status, unreadCount, lastCursor, error, pause, resume, retry };
}

export default useLiveLogs;
