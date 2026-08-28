import React from 'react';
import { Box, Text } from 'ink';
import { ToolCard } from './ToolCard.js';
import type { ToolBlock } from '../state/cli-state.js';

export type ProgressItemValue =
  | { readonly kind: 'tool'; readonly tool: ToolBlock }
  | { readonly kind: 'todo' | 'hypothesis' | 'child'; readonly id: string; readonly status: string; readonly summary: string };

export interface ProgressItemProps { readonly item: ProgressItemValue; readonly noColor?: boolean }

export function ProgressItem({ item, noColor }: ProgressItemProps): React.ReactElement {
  if (item.kind === 'tool') return <ToolCard tool={item.tool} noColor={noColor} />;
  return <Box><Text>{item.kind} [{item.status}] {item.id}: {item.summary}</Text></Box>;
}
