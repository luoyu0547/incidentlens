export interface LogRouteSearch {
  readonly cursor?: string;
  readonly severity?: string;
}

export interface LogViewerProps {
  readonly serviceId: string;
  readonly targetId: string;
  readonly initialSearch: LogRouteSearch;
}

/**
 * Stable integration boundary for the future generated log reader. It deliberately
 * renders no log data, preserving the service view's read-only surface without
 * presenting synthetic records.
 */
export function LogViewerPlaceholder({ serviceId, targetId }: LogViewerProps) {
  return (
    <section aria-label="日志查看器" data-service-id={serviceId} data-target-id={targetId}>
      <h3>日志查看器</h3>
      <p>日志查看器将在此处加载</p>
    </section>
  );
}
