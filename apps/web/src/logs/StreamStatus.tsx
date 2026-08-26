export interface StreamStatusProps { readonly mode: 'history' | 'live'; readonly isFetching?: boolean; readonly error?: unknown; }

export function StreamStatus({ mode, isFetching, error }: StreamStatusProps) {
  if (error) return <p role="status">日志加载失败，筛选条件已保留</p>;
  if (isFetching) return <p role="status">正在同步日志…</p>;
  return <p role="status">{mode === 'live' ? '实时日志' : '历史日志'}</p>;
}
