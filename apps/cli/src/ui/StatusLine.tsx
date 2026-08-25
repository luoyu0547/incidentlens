/**
 * Status line component for IncidentLens CLI.
 *
 * Shows stream connection status and pending approvals.
 */

import React from 'react';
import { Box, Text } from 'ink';

interface StatusLineProps {
  readonly streamConnected: boolean;
  readonly pendingApprovals: number;
}

/**
 * Status line component.
 */
export function StatusLine({
  streamConnected,
  pendingApprovals,
}: StatusLineProps): React.ReactElement {
  return (
    <Box>
      <Text color={streamConnected ? 'green' : 'red'}>
        {streamConnected ? '●' : '○'}
      </Text>
      <Text color="gray"> Connected</Text>
      {pendingApprovals > 0 && (
        <>
          <Text color="gray"> | </Text>
          <Text color="yellow">{pendingApprovals} pending approval(s)</Text>
        </>
      )}
    </Box>
  );
}
