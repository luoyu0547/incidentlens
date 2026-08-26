import type { LogRecordView } from '@incidentlens/protocol';
import type { CSSProperties } from 'react';
import { EvidenceMarker } from './EvidenceMarker';
import { presentLogBody } from './log-presentation';
import { StackTrace } from './StackTrace';
import { StructuredJson } from './StructuredJson';

export interface LogRowProps {
  readonly record: LogRecordView;
  readonly measureRef?: (element: HTMLElement | null) => void;
  readonly style?: CSSProperties;
}

export function LogRow({ record, measureRef, style }: LogRowProps) {
  const body = presentLogBody(record);
  return (
    <li ref={measureRef} data-log-id={record.log_id} style={{ listStyle: 'none', ...style }}>
      <time dateTime={record.occurred_at}>{record.occurred_at}</time>
      <strong className={`log-viewer__severity log-viewer__severity--${record.severity}`}>{record.severity}</strong>
      <span>{body.kind === 'json' ? <details><summary>{body.summary}</summary><StructuredJson value={body.value} /></details> : body.kind === 'stack' ? <details><summary>{body.headline}</summary><StackTrace headline={body.headline} lines={body.lines} /></details> : body.text}</span>
      {record.fields?.redacted === true && <span aria-label="已脱敏">已脱敏</span>}
      <EvidenceMarker record={record} />
    </li>
  );
}
