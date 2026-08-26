import type { LogRecordView, ServiceLogQuery } from '@incidentlens/protocol';
import { useEffect, useMemo, useState } from 'react';
import { readonlyClient } from '../api/client';
import { EmptyState } from '../shared/EmptyState';
import { ErrorNotice } from '../shared/ErrorNotice';
import { LogToolbar } from './LogToolbar';
import { StreamStatus } from './StreamStatus';
import { VirtualLogViewport } from './VirtualLogViewport';
import { logSearchError, type LogRouteSearch } from './log-search';
import useLiveLogs from './useLiveLogs';
import { useLogAnchor } from './useLogAnchor';
import { useLogHistory } from './useLogHistory';

export interface LogViewerProps { readonly serviceId: string; readonly targetId: string; readonly initialSearch: LogRouteSearch; readonly onSearchChange?: (search: LogRouteSearch) => void; }

export function LogViewer({ serviceId, targetId, initialSearch, onSearchChange }: LogViewerProps) {
  const [anchoredRecords, setAnchoredRecords] = useState<readonly LogRecordView[]>([]);
  const search = initialSearch;
  const update = (patch: Partial<LogRouteSearch>) => onSearchChange?.({ ...search, ...patch });
  useEffect(() => { setAnchoredRecords([]); }, [search.anchor]);
  const invalidRange = logSearchError(search);
  const history = useLogHistory(serviceId, search);
  const live = useLiveLogs(serviceId, { ...search, target: search.target ?? targetId }, { enabled: search.mode === 'live' });
  const recordsBase = search.mode === 'live' ? live.records : (history.data?.pages.flatMap((page) => page.items) ?? []);
  const records = [...recordsBase, ...anchoredRecords.filter((r) => !recordsBase.some((x) => x.log_id === r.log_id))];
  const anchor = useMemo(() => search.anchor ? { service: serviceId, log_id: search.anchor, context: search.context } : null, [search.anchor, search.context, serviceId]);
  const scrollTo = (logId: string) => document.querySelector(`[data-log-id="${CSS.escape(logId)}"]`)?.scrollIntoView({ block: 'center' });
  const fetchContext = async (locator: { context?: number | null }): Promise<readonly LogRecordView[]> => {
    const query: ServiceLogQuery = { limit: locator.context ?? search.context, severity: search.levels[0], source_ref: search.target ?? targetId };
    return (await readonlyClient.getServiceLogs(serviceId, query)).items;
  };
  const locator = useLogAnchor({ locator: anchor, currentService: serviceId, records, fetchContext, onRecords: setAnchoredRecords, scrollTo });
  useEffect(() => { if (search.anchor && !locator.present) void locator.locate(); }, [search.anchor, locator.present, locator.locate]);
  const error = search.mode === 'live' ? live.error : history.error;
  const status = search.mode === 'live' ? live.status : undefined; <section className="log-viewer" aria-label="日志查看器">
    <h2>日志</h2>
    <LogToolbar search={search} onChange={update} />
    <StreamStatus mode={search.mode} isFetching={search.mode === 'history' ? history.isFetching : status === 'backfilling'} error={error} />
    {search.mode === 'live' && <p className="log-viewer__status" role="status">{status === 'gap' ? '正在恢复日志间隙…' : status === 'reconnecting' ? '正在重新连接日志流…' : status === 'error' ? '日志流认证失败或无法恢复。' : status === 'live' ? '日志流已连接' : '正在连接日志流…'}</p>}
    {search.mode === 'history' && history.hasNextPage && <button type="button" onClick={() => void history.fetchNextPage()} disabled={history.isFetchingNextPage}>加载更早日志</button>}
    {invalidRange ? <ErrorNotice title="筛选条件无效" message={invalidRange} /> : null}
    {error ? <ErrorNotice message="日志加载失败，筛选条件已保留。" /> : null}
    {!invalidRange && !error && !history.isPending && records.length === 0 ? <EmptyState title="暂无日志" description="当前筛选条件没有匹配的日志记录。" /> : null}
    {records.length > 0 ? <VirtualLogViewport records={records} follow={search.follow} onLocate={scrollTo} paused={search.mode === 'live' && status === 'paused'} unreadCount={live.unreadCount} onResume={search.mode === 'live' ? () => live.resume() : undefined} onPrepend={search.mode === 'history' && history.hasNextPage ? () => void history.fetchNextPage() : undefined} /> : null}
    {locator.anchorId && locator.present && <p role="status">已定位日志 {locator.anchorId}</p>}
  </section>;
}
