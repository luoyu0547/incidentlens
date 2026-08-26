import type { LogRecordView } from '@incidentlens/protocol';
import type { CSSProperties } from 'react';

export interface LogRowProps {
  readonly record: LogRecordView;
  readonly measureRef?: (element: HTMLElement | null) => void;
  readonly style?: CSSProperties;
}

export function LogRow({ record, measureRef, style }: LogRowProps) {
  return (
    <li ref={measureRef} data-log-id={record.log_id} style={{ listStyle: 'none', ...style }}>
      <time dateTime={record.occurred_at}>{record.occurred_at}</time>{' '}
      <strong>{record.severity}</strong>{' '}
      <span>{record.message}</span>
    </li>
  );
}
