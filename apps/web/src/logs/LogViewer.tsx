import type { LogRecordView } from '@incidentlens/protocol';
import { useState } from 'react';
import { useLogHistory } from './useLogHistory';
import { LogToolbar } from './LogToolbar';
import { StreamStatus } from './StreamStatus';
import { EmptyState } from '../shared/EmptyState';
import { Timestamp } from '../shared/Timestamp';
import { ErrorNotice } from '../shared/ErrorNotice';
import type { LogRouteSearch } from './log-search';

export interface LogViewerProps { readonly serviceId: string; readonly targetId: string; readonly initialSearch: LogRouteSearch; readonly onSearchChange?: (search: LogRouteSearch) => void; }

export function LogViewer({ serviceId, initialSearch, onSearchChange }: LogViewerProps) {
  const [search, setSearch] = useState(initialSearch);
  const update = (patch: Partial<LogRouteSearch>) => {
    const next = { ...search, ...patch };
    setSearch(next);
    onSearchChange?.(next);
  };
  const history = useLogHistory(serviceId, search);
  const records: LogRecordView[] = history.data?.pages.flatMap((page) => page.items) ?? [];
  const invalidRange = logError(search);
  return <section className="log-viewer" aria-label="日志查看器">
    <LogToolbar search={search} onChange={update} />
    <StreamStatus mode={search.mode} isFetching={history.isFetching} error={history.error} />
    {search.mode === 'history' && history.hasNextPage && <button type="button" onClick={() => void history.fetchNextPage()} disabled={history.isFetchingNextPage}>加载更早日志</button>}
    {invalidRange ? <ErrorNotice title="筛选条件无效" message={invalidRange} /> : null}
    {history.error ? <ErrorNotice message="日志加载失败，筛选条件已保留。" /> : null}
    {!history.isPending && !history.error && !invalidRange && records.length === 0 ? <EmptyState title="暂无日志" description="当前筛选条件没有匹配的日志记录。" /> : null}
    {records.length > 0 ? <div className="log-viewer__viewport" tabIndex={0} role="region" aria-label="可滚动日志记录">
      <ol className="log-viewer__records" aria-label="日志记录">{records.map((record) => <li key={record.log_id}><Timestamp value={record.occurred_at} timeZone={undefined} /> <strong className={`log-viewer__severity log-viewer__severity--${record.severity}`}>{record.severity}</strong> <span>{record.message}</span></li>)}</ol>
    </div> : null}
  </section>;
}
function logError(search: LogRouteSearch) { return search.from && search.to && Date.parse(search.from) > Date.parse(search.to) ? '开始时间必须早于结束时间' : undefined; }
