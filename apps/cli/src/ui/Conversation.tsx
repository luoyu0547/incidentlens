/**
 * Conversation component for IncidentLens CLI.
 *
 * Renders the message history in a single-column flow.
 */

import React, { useEffect, useState } from 'react';
import { Box, Text } from 'ink';
import type { ConversationItem, TextBlock, ToolBlock } from '../state/cli-state.js';

interface ConversationProps {
  readonly messages: readonly ConversationItem[];
}

/**
 * Conversation component.
 */
export function Conversation({ messages }: ConversationProps): React.ReactElement | null {
  // Streaming can briefly create an empty text block before its first delta.
  // Do not render that placeholder as a large empty bordered card.
  const visibleMessages = messages.filter(
    (item) =>
      (item.kind !== 'text' || isRenderableAgentText(item.content))
      && (item.kind !== 'tool' || item.toolName !== 'todo_write'),
  );

  if (visibleMessages.length === 0) {
    return null;
  }

  return (
    <Box flexDirection="column" marginTop={1}>
      {visibleMessages.map((item, index) => (
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
    case 'user':
      return `user-${item.messageId}`;
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
    case 'user':
      return (
        <Box marginBottom={1} paddingLeft={1} borderStyle="single" borderColor="magenta">
          <Text color="magenta" bold>❯ </Text>
          <Text color="white">{item.content}</Text>
        </Box>
      );
    case 'tool':
      return <ToolBlockView block={item} />;
    case 'approval':
      return (
        <Box marginBottom={1} paddingX={1} borderStyle="single" borderColor="yellow">
          <Text color="yellow" bold>[Approval: {item.approvalId}]</Text>
        </Box>
      );
    case 'system':
      return (
        <Box marginBottom={1} paddingLeft={1}>
          <Text color={item.content.includes('失败') ? 'red' : item.content.includes('取消') ? 'yellow' : 'green'} bold>
            {item.content.includes('失败') ? '✗ ' : item.content.includes('完成') ? '✓ ' : '· '}
          </Text>
          <Text color={item.content.includes('失败') ? 'red' : item.content.includes('完成') ? 'white' : 'gray'}>{item.content}</Text>
        </Box>
      );
  }
}

/**
 * Render a text block.
 */
function TextBlockView({ block }: { block: TextBlock }): React.ReactElement {
  const formatted = formatAgentText(block.content);
  const shouldAnimate = block.finalized !== undefined;
  const [visibleLength, setVisibleLength] = useState(
    shouldAnimate ? 0 : formatted.length,
  );

  useEffect(() => {
    if (!shouldAnimate) {
      setVisibleLength(formatted.length);
      return;
    }
    setVisibleLength((current) => Math.min(current, formatted.length));
    const timer = setInterval(() => {
      setVisibleLength((current) => {
        if (current >= formatted.length) {
          clearInterval(timer);
          return current;
        }
        return Math.min(formatted.length, current + 8);
      });
    }, 24);
    return () => clearInterval(timer);
  }, [formatted, shouldAnimate]);

  return (
    <Box marginBottom={1} paddingLeft={1} borderStyle="single" borderColor={block.finalized ? 'cyan' : 'gray'}>
      <Text color={block.finalized ? undefined : 'gray'}>{formatted.slice(0, visibleLength)}</Text>
    </Box>
  );
}

function isRenderableAgentText(content: unknown): content is string {
  if (typeof content !== 'string' || content.trim().length === 0) return false;
  try {
    const value = JSON.parse(content) as Record<string, unknown>;
    const empty = Array.isArray(value.conclusions) && value.conclusions.length === 0
      && Array.isArray(value.hypotheses) && value.hypotheses.length === 0
      && value.delegation == null && value.stop == null;
    return !empty;
  } catch {
    return true;
  }
}

function formatAgentText(content: string): string {
  try {
    const value = JSON.parse(content) as Record<string, any>;
    const summaries = [
      ...(Array.isArray(value.conclusions) ? value.conclusions.map((item) => item?.summary).filter(Boolean) : []),
      ...(Array.isArray(value.hypotheses) ? value.hypotheses.map((item) => item?.summary).filter(Boolean) : []),
      value.stop?.summary,
    ].filter((item): item is string => typeof item === 'string');
    if (summaries.length > 0) return summaries.join(' ');
  } catch {
    // Non-JSON assistant text is already user-readable.
  }
  return content;
}

/**
 * Render a tool block.
 */
function ToolBlockView({ block }: { block: ToolBlock }): React.ReactElement {
  const statusColor = getStatusColor(block.status);
  const statusIcon = getStatusIcon(block.status);
  const statusLabel = getStatusLabel(block.status);

  return (
    <Box marginBottom={1}>
      <Text color={statusColor}>{statusIcon}</Text>
      <Text bold> {getToolLabel(block.toolName)}</Text>
      <Text color="gray"> · {statusLabel}</Text>
      {block.summary && <Text color="gray"> — {block.summary}</Text>}
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
    case 'waiting_approval':
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
    case 'waiting_approval':
      return '⚠';
    case 'succeeded':
      return '●';
    case 'failed':
      return '✗';
    case 'uncertain':
      return '?';
  }
}

function getStatusLabel(status: ToolBlock['status']): string {
  switch (status) {
    case 'proposed':
      return '计划中';
    case 'running':
      return '执行中';
    case 'waiting_approval':
      return '等待审批';
    case 'succeeded':
      return '完成';
    case 'failed':
      return '失败';
    case 'uncertain':
      return '状态待确认';
  }
}

function getToolLabel(toolName: string): string {
  const labels: Record<string, string> = {
    registry_info: '调查范围',
    host_metrics: '主机状态',
    host_read: '读取主机文件',
    host_list: '列出主机目录',
    host_search: '搜索主机文件',
    host_stat: '读取文件信息',
    container_read: '读取容器文件',
    container_list: '列出容器目录',
    container_search: '搜索容器文件',
    container_stat: '读取容器文件信息',
    log_query: '查询日志',
    log_search: '搜索日志',
    log_context: '读取日志上下文',
    shell_exec: '受控命令',
  };
  return labels[toolName] ?? toolName.replaceAll('_', ' ');
}
