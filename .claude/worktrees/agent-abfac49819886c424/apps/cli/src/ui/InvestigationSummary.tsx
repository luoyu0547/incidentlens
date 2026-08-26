import React from 'react';
import { Box, Text } from 'ink';
import type { InvestigationSummaryView, EvidenceSnippetView, HypothesisSummaryView } from '@incidentlens/protocol';
import { ProgressItem, type ProgressItemData } from './ProgressItem.js';

export interface InvestigationSummaryProps {
  readonly summary: Pick<InvestigationSummaryView, 'investigation_id' | 'status' | 'conclusion' | 'evidence' | 'hypotheses'> & {
    readonly todos?: readonly { id: string; title: string; status: string }[];
    readonly children?: readonly { id: string; title?: string; status: string }[];
  };
  readonly noColor?: boolean;
  readonly maxSummaryLength?: number;
}

export function InvestigationSummary({ summary, noColor = process.env.NO_COLOR !== undefined, maxSummaryLength = 160 }: InvestigationSummaryProps): React.ReactElement {
  const items: ProgressItemData[] = [
    ...(summary.todos ?? []).map((item) => ({ kind: 'todo' as const, ...item })),
    ...(summary.hypotheses ?? []).map((item: HypothesisSummaryView) => ({ kind: 'hypothesis' as const, id: item.hypothesis_id, summary: item.summary, status: item.status })),
    ...(summary.evidence ?? []).map((item: EvidenceSnippetView) => ({ kind: 'evidence' as const, id: item.evidence_ref_id, summary: item.summary })),
    ...(summary.children ?? []).map((item) => ({ kind: 'child' as const, ...item })),
  ];
  const conclusion = summary.conclusion?.summary;
  return <Box flexDirection="column"><Text bold>Investigation {summary.investigation_id} [{summary.status}]</Text>{conclusion && <Text>Conclusion: {conclusion}</Text>}{items.map((item, index) => <ProgressItem key={`${item.kind}-${item.id}-${index}`} item={item} noColor={noColor} maxSummaryLength={maxSummaryLength} />)}</Box>;
}
