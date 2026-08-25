/**
 * Conversation component for IncidentLens CLI.
 *
 * Renders the message history in a single-column flow.
 */

import React from 'react';
import { Box, Text } from 'ink';
import type { ConversationItem, TextBlock, ToolBlock } from '../state/cli-state.js';

interface ConversationProps {
  readonly messages: readonly ConversationItem[];
}

/**
 * Conversation component.
 */
export function Conversation({ messages }: ConversationProps): React.ReactElement | null {
  if (messages.length === 0) {
    return null;
  }

  return (
    <Box flexDirection="column">
      {messages.map((item, index) => (
        <ConversationItem key={getItemKey(item, index)} item={item} />
      ))}
    </Box>
  );
}

/**
 * Get a unique key for a conversation item.
 */
function getItemKey(item: ConversationItem, index: number): string {
  switch (item.kind) {
    case 'text':
      return `text-${item.messageId}-${item.blockId}`;
    case 'tool':
      return `tool-${item.toolId}`;
    case 'approval':
      return `approval-${item.approvalId}`;
    case 'system':
      return `system-${index}`;
  }
}

/**
 * Render a single conversation item.
 */
function ConversationItem({ item }: { item: ConversationItem }): React.ReactElement {
  switch (item.kind) {
    case 'text':
      return <TextBlockView block={item} />;
    case 'tool':
      return <ToolBlockView block={item} />;
    case 'approval':
      return (
        <Box>
          <Text color="yellow">[Approval: {item.approvalId}]</Text>
        </Box>
      );
    case 'system':
      return (
        <Box>
          <Text color="gray">{item.content}</Text>
        </Box>
      );
  }
}

/**
 * Render a text block.
 */
function TextBlockView({ block }: { block: TextBlock }): React.ReactElement {
  return (
    <Box>
      <Text>{block.content}</Text>
    </Box>
  );
}

/**
 * Render a tool block.
 */
function ToolBlockView({ block }: { block: ToolBlock }): React.ReactElement {
  const statusColor = getStatusColor(block.status);
  const statusIcon = getStatusIcon(block.status);

  return (
    <Box>
      <Text color={statusColor}>{statusIcon}</Text>
      <Text> {block.toolName}</Text>
      {block.error && <Text color="red"> ({block.error})</Text>}
    </Box>
  );
}

/**
 * Get color for tool status.
 */
function getStatusColor(status: ToolBlock['status']): string {
  switch (status) {
    case 'proposed':
      return 'gray';
    case 'running':
      return 'yellow';
    case 'succeeded':
      return 'green';
    case 'failed':
      return 'red';
    case 'uncertain':
      return 'magenta';
  }
}

/**
 * Get icon for tool status.
 */
function getStatusIcon(status: ToolBlock['status']): string {
  switch (status) {
    case 'proposed':
      return '○';
    case 'running':
      return '◎';
    case 'succeeded':
      return '●';
    case 'failed':
      return '✗';
    case 'uncertain':
      return '?';
  }
}
