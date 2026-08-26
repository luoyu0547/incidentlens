import type { LogRecordView } from '@incidentlens/protocol';
import { useEffect, useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { LogRow } from './LogRow';

export interface VirtualLogViewportProps {
  readonly records: readonly LogRecordView[];
  readonly paused?: boolean;
  readonly unreadCount?: number;
  readonly onResume?: () => void;
  readonly onPrepend?: () => void;
  readonly follow?: boolean;
  readonly onLocate?: (logId: string) => void;
  readonly className?: string;
}

export function VirtualLogViewport({ records, paused = false, unreadCount = 0, onResume, onPrepend, follow = true, onLocate, className }: VirtualLogViewportProps) {
  const parentRef = useRef<HTMLDivElement>(null);
  const previousFirst = useRef<string | undefined>(records[0]?.log_id);
  const previousCount = useRef(records.length);
  const [following, setFollowing] = useState(true);
  const virtualizer = useVirtualizer({
    count: records.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 28,
    overscan: 8,
    measureElement: (element) => element.getBoundingClientRect().height,
  });

  useEffect(() => {
    const first = records[0]?.log_id;
    if (first && previousFirst.current && first !== previousFirst.current && records.length > previousCount.current) {
      const previousIndex = records.findIndex((record) => record.log_id === previousFirst.current);
      if (previousIndex >= 0) virtualizer.scrollToIndex(previousIndex, { align: 'start' });
    }
    previousFirst.current = first;
    previousCount.current = records.length;
  }, [records, virtualizer]);

  useEffect(() => {
    if (follow && following && !paused && records.length > 0) virtualizer.scrollToIndex(records.length - 1, { align: 'end' });
  }, [follow, following, paused, records.length, virtualizer]);

  const locateLatest = () => { setFollowing(true); if (records.length) virtualizer.scrollToIndex(records.length - 1, { align: 'end' }); onResume?.(); };
  return (
    <section aria-label="日志记录视口" className={className}>
      {onPrepend && <button type="button" onClick={onPrepend}>加载更早日志</button>}
      {(paused || unreadCount > 0 || !following) && <button type="button" onClick={locateLatest}>定位最新{unreadCount > 0 ? ` (${unreadCount})` : ''}</button>}
      <div className="log-viewer__viewport" ref={parentRef} onScroll={() => {
        const element = parentRef.current;
        if (!element) return;
        setFollowing(element.scrollHeight - element.scrollTop - element.clientHeight < 32);
      }} style={{ height: 360, overflow: 'auto' }}>
        <ol className="log-viewer__records" aria-label="日志记录" style={{ height: virtualizer.getTotalSize(), position: 'relative', margin: 0, padding: 0 }}>
          {virtualizer.getVirtualItems().map((item) => {
            const record = records[item.index];
            return <LogRow key={record.log_id} record={record} measureRef={virtualizer.measureElement} onLocate={onLocate} style={{ position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${item.start}px)` }} />;
          })}
        </ol>
      </div>
    </section>
  );
}
