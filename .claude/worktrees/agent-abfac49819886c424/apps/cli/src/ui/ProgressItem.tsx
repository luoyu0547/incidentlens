import React from 'react';
import { Box, Text } from 'ink';
import { ToolCard, type ToolCardProps } from './ToolCard.js';

export type ProgressItemData =
  | ({ readonly kind: 'tool' } & ToolCardProps['tool'])
  | { readonly kind: 'todo'; readonly id: string; readonly title: string; readonly status: string }
  | { readonly kind: 'hypothesis'; readonly id: string; readonly summary: string; readonly status: string }
  | { readonly kind: 'evidence'; readonly id: string; readonly summary?: string }
  | { readonly kind: 'child'; readonly id: string; readonly title?: string; readonly status: string };

export interface ProgressItemProps {
  readonly item: ProgressItemData;
  readonly noColor?: boolean;
  readonly maxSummaryLength?: number;
}

const bounded = (value: string | undefined, limit: number): string | undefined => {
  if (!value) return undefined;
  return value.length > limit ? `${value.slice(0, Math.max(0, limit - 1))}…` : value;
};

export function ProgressItem({ item, noColor = process.env.NO_COLOR !== undefined, maxSummaryLength = 160 }: ProgressItemProps): React.ReactElement {
  if (item.kind === 'tool') return <ToolCard tool={item} noColor={noColor} maxSummaryLength={maxSummaryLength} />;
  const icon = item.status === 'succeeded' || item.status === 'completed' ? '●' : item.status === 'failed' ? '✗' : item.status === 'uncertain' ? '?' : '○';
  const color = noColor ? undefined : item.status === 'failed' ? 'red' : item.status === 'uncertain' ? 'magenta' : item.status === 'succeeded' || item.status === 'completed' ? 'green' : 'yellow';
  const text = item.kind === 'todo' ? item.title : item.kind === 'hypothesis' ? item.summary : item.kind === 'child' ? (item.title ?? item.id) : item.id;
  const detail = item.kind === 'evidence' ? item.summary : undefined;
  return <Box flexDirection="column"><Box><Text color={color}>{icon}</Text><Text> {item.kind} {bounded(text, maxSummaryLength)} [{item.status ?? 'recorded'}]</Text></Box>{detail && <Text color={noColor ? undefined : 'gray'}>  {bounded(detail, maxSummaryLength)}</Text>}</Box>;
}
