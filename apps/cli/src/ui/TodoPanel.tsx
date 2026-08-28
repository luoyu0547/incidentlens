import React from 'react';
import { Box, Text } from 'ink';
import type { TodoItemState } from '../state/cli-state.js';

export function TodoPanel({
  todos,
}: {
  readonly todos: readonly TodoItemState[];
}): React.ReactElement | null {
  if (todos.length === 0) return null;

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor="blue"
      paddingX={1}
      marginTop={1}
    >
      <Text bold color="blue">调查计划</Text>
      {todos.map((todo) => {
        const icon = todo.status === 'completed' ? '✓' : todo.status === 'in_progress' ? '◉' : '○';
        const color = todo.status === 'completed' ? 'green' : todo.status === 'in_progress' ? 'yellow' : 'gray';
        return (
          <Text key={todo.todoId} color={color}>
            {icon} {formatTodoContent(todo.content)}
          </Text>
        );
      })}
    </Box>
  );
}

function formatTodoContent(content: string): string {
  return content
    .replaceAll('registry_info', '调查范围')
    .replaceAll('host_metrics load', '主机负载')
    .replaceAll('host_metrics memory', '内存')
    .replaceAll('host_metrics disk', '磁盘')
    .replace(/（(调查范围|主机负载|内存|磁盘)）/g, '：$1')
    .replace(/\((调查范围|主机负载|内存|磁盘)\)/g, '：$1');
}
