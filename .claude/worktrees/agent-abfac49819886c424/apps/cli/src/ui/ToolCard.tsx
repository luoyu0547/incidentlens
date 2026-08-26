import React from 'react';
import { Box, Text } from 'ink';
import type { ToolBlock } from '../state/cli-state.js';

export type ToolCardStatus = ToolBlock['status'];

export interface ToolCardProps {
  readonly tool: Pick<ToolBlock, 'toolId' | 'toolName' | 'status' | 'summary' | 'error'>;
  readonly noColor?: boolean;
  readonly maxSummaryLength?: number;
}

const STATUS: Record<ToolCardStatus, { symbol: string; label: string; color: string }> = {
  proposed: { symbol: '○', label: 'proposed', color: 'gray' },
  running: { symbol: '◎', label: 'running', color: 'yellow' },
  succeeded: { symbol: '●', label: 'succeeded', color: 'green' },
  failed: { symbol: '✗', label: 'failed', color: 'red' },
  uncertain: { symbol: '?', label: 'uncertain', color: 'magenta' },
};

const safeText = (value: string | undefined, limit: number): string | undefined => {
  if (!value) return undefined;
  return value.length > limit ? `${value.slice(0, Math.max(0, limit - 1))}…` : value;
};

export function ToolCard({ tool, noColor = process.env.NO_COLOR !== undefined, maxSummaryLength = 160 }: ToolCardProps): React.ReactElement {
  const status = STATUS[tool.status];
  const summary = safeText(tool.summary, maxSummaryLength);
  const error = safeText(tool.error, maxSummaryLength);
  const color = noColor ? undefined : status.color;

  return (
    <Box flexDirection="column">
      <Box>
        <Text color={color}>{status.symbol}</Text>
        <Text> {tool.toolName} </Text>
        <Text color={color}>[{status.label}]</Text>
      </Box>
      {summary && <Text color={noColor ? undefined : 'gray'}>  {summary}</Text>}
      {error && <Text color={noColor ? undefined : tool.status === 'uncertain' ? 'magenta' : 'red'}>  {error}</Text>}
    </Box>
  );
}

export function toolStatusLabel(status: ToolCardStatus): string {
  return STATUS[status].label;
}
