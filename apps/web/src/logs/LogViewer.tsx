import type { LogRecordView } from '@incidentlens/protocol';
import { useState } from 'react';
import { useLogHistory } from './useLogHistory';
import { LogToolbar } from './LogToolbar';
import { StreamStatus } from './StreamStatus';
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
  return <section aria-label="日志查看器">
    <LogToolbar search={search} onChange={update} />
    <StreamStatus mode={search.mode} isFetching={history.isFetching} error={history.error} />
    {search.mode === 'history' && history.hasNextPage && <button type="button" onClick={() => void history.fetchNextPage()} disabled={history.isFetchingNextPage}>加载更早日志</button>}
    {logError(search) && <p role="alert">{logError(search)}</p>}
    <ol aria-label="日志记录">{records.map((record) => <li key={record.log_id}><time dateTime={record.occurred_at}>{record.occurred_at}</time> <strong>{record.severity}</strong> {record.message}</li>)}</ol>
  </section>;
}
function logError(search: LogRouteSearch) { return search.from && search.to && Date.parse(search.from) > Date.parse(search.to) ? '开始时间必须早于结束时间' : undefined; }
