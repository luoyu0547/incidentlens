import React from 'react';
import { Box, Text } from 'ink';
import type { UsageState } from '../state/cli-state.js';

export function UsageLine({ usage }: { readonly usage: UsageState }): React.ReactElement | null {
  if (usage.rounds === 0) return null;
  return (
    <Box paddingX={1} marginTop={1}>
      <Text color="gray">
        Tokens  ↑ {usage.inputTokens.toLocaleString()}  ↓ {usage.outputTokens.toLocaleString()}
        {'  ·  '}{usage.rounds} round{usage.rounds === 1 ? '' : 's'}
      </Text>
    </Box>
  );
}
