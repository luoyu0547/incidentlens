import type { LogRouteSearch } from './log-search';

export interface LogToolbarProps { readonly search: LogRouteSearch; readonly onChange: (patch: Partial<LogRouteSearch>) => void; }

export function LogToolbar({ search, onChange }: LogToolbarProps) {
  return <form aria-label="日志筛选" onSubmit={(event) => event.preventDefault()}>
    <label>搜索 <input aria-label="日志搜索" value={search.q ?? ''} onChange={(e) => onChange({ q: e.target.value || undefined })} /></label>
    <label>级别 <select aria-label="日志级别" value={search.levels[0] ?? ''} onChange={(e) => onChange({ levels: e.target.value ? [e.target.value as LogRouteSearch['levels'][number]] : [] })}>
      <option value="">全部</option><option value="trace">trace</option><option value="debug">debug</option><option value="info">info</option><option value="warn">warn</option><option value="error">error</option><option value="critical">critical</option>
    </select></label>
    <label><input type="checkbox" checked={search.follow} onChange={(e) => onChange({ follow: e.target.checked })} /> 跟随最新</label>
  </form>;
}
