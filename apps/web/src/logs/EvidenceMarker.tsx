import type { CSSProperties } from 'react';
import type { LogRecordView } from '@incidentlens/protocol';

export interface EvidenceMarkerProps {
  readonly record: LogRecordView;
  readonly active?: boolean;
  readonly onLocate?: () => void;
}

/** Marker attached to the exact server-identified log row. */
export function EvidenceMarker({ record, active = false, onLocate }: EvidenceMarkerProps) {
  return <span data-cursor={record.cursor} data-active={active || undefined} style={markerStyle}>
    <button type="button" onClick={onLocate} aria-label={`定位日志 ${record.log_id}`}>证据</button>
  </span>;
}

const markerStyle: CSSProperties = { marginInlineStart: 8, fontSize: '0.8em' };
