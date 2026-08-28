import type { ReactNode } from 'react';

export interface HighlightSegment { readonly text: string; readonly highlighted: boolean; }

export function highlightSegments(text: string, query: string): readonly HighlightSegment[] {
  if (!query) return [{ text, highlighted: false }];
  const segments: HighlightSegment[] = [];
  let cursor = 0;
  while (cursor < text.length) {
    const index = text.toLocaleLowerCase().indexOf(query.toLocaleLowerCase(), cursor);
    if (index < 0) { segments.push({ text: text.slice(cursor), highlighted: false }); break; }
    if (index > cursor) segments.push({ text: text.slice(cursor, index), highlighted: false });
    segments.push({ text: text.slice(index, index + query.length), highlighted: true });
    cursor = index + query.length;
  }
  return segments;
}

export function HighlightedText({ text, query }: { readonly text: string; readonly query: string }): ReactNode {
  return <>{highlightSegments(text, query).map((segment, index) => segment.highlighted ? <mark key={index}>{segment.text}</mark> : <span key={index}>{segment.text}</span>)}</>;
}
