import React from 'react';
import { Box, Text } from 'ink';
import type { ToolBlock } from '../state/cli-state.js';

export interface ToolCardProps {
  readonly tool: ToolBlock;
  readonly noColor?: boolean;
}

const colors: Record<ToolBlock['status'], string> = {
  proposed: 'gray',
  running: 'yellow',
  succeeded: 'green',
  failed: 'red',
  uncertain: 'magenta',
};

const symbols: Record<ToolBlock['status'], string> = {
  proposed: '○', running: '◎', succeeded: '●', failed: '✗', uncertain: '?',
};

function colorEnabled(noColor?: boolean): boolean {
  return !(noColor ?? process.env.NO_COLOR !== undefined);
}

export function ToolCard({ tool, noColor }: ToolCardProps): React.ReactElement {
  const colored = colorEnabled(noColor);
  const status = tool.status === 'uncertain' ? 'UNCERTAIN' : tool.status.toUpperCase();
  return (
    <Box flexDirection="column" borderStyle="round" borderColor={colored ? colors[tool.status] : undefined} paddingX={1}>
      <Text color={colored ? colors[tool.status] : undefined}>
        {symbols[tool.status]} {tool.toolName} — {status}
      </Text>
      {tool.summary && <Text>{tool.summary}</Text>}
      {tool.error && <Text>{tool.status === 'uncertain' ? 'Uncertain result: ' : 'Error: '}{tool.error}</Text>}
    </Box>
  );
}

export { colorEnabled };
