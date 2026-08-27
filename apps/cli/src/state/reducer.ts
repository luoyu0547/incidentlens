/**
 * Pure CLI projection reducer for IncidentLens.
 *
 * Consumes parsed known envelopes and authoritative HTTP snapshots
 * to produce immutable state updates.
 */

import type {
  CliState,
  CliAction,
  ConversationItem,
  TextBlock,
  ToolBlock,
  ToolEventSnapshot,
  StreamStatus,
} from './cli-state.js';

/**
 * Create the initial application state.
 */
export function createInitialState(): CliState {
  return {
    bootstrap: 'loading',
    messages: [],
    operations: {},
    approvals: {},
    todos: [],
    usage: { rounds: 0, inputTokens: 0, outputTokens: 0 },
    activity: { kind: 'idle' },
    stream: {
      connected: false,
      lastSequence: 0,
    },
    input: {
      focused: true,
      value: '',
    },
    overlay: { kind: 'none' },
  };
}

/**
 * State reducer for the CLI application.
 *
 * Rules:
 * - Only consumes parsed known envelopes and HTTP snapshots
 * - Tracks last committed sequence separately from visible items
 * - Does not store entire raw envelopes after application
 * - Rejects duplicate and old sequence events
 */
export function reducer(state: CliState, action: CliAction): CliState {
  switch (action.type) {
    case 'bootstrap_complete':
      return { ...state, bootstrap: action.state };

    case 'set_target':
      return { ...state, target: action.target };

    case 'clear_target':
      return { ...state, target: undefined };

    case 'set_session':
      // A newly-created session starts a fresh transcript. Keeping rows from
      // the previous session makes an old running tool appear to belong to
      // the new prompt, which is especially misleading during reconnects.
      if (state.session?.session_id && state.session.session_id !== action.session.session_id) {
        return { ...state, session: action.session, messages: [], operations: {}, approvals: {}, todos: [], usage: { rounds: 0, inputTokens: 0, outputTokens: 0 }, activity: { kind: 'idle' }, activeOperationId: undefined };
      }
      return { ...state, session: action.session };

    case 'set_approval':
      return {
        ...state,
        approvals: { ...state.approvals, [action.approval.approval_id]: action.approval },
      };

    case 'clear_approvals':
      return { ...state, approvals: {} };

    case 'update_operation':
      return updateOperation(state, action.operation);

    case 'stream_event':
      return handleStreamEvent(state, action.event);

    case 'gap_snapshot':
      return handleGapSnapshot(state, action.snapshot);

    case 'set_stream_status':
      return {
        ...state,
        stream: { ...state.stream, ...action.status },
      };

    case 'set_input':
      return {
        ...state,
        input: { ...state.input, ...action.input },
      };

    case 'set_overlay':
      return { ...state, overlay: action.overlay };

    case 'system_message':
      return {
        ...state,
        messages: [
          ...state.messages,
          {
            kind: 'system',
            content: action.content,
            timestamp: action.timestamp,
          },
        ],
      };

    case 'user_message':
      return {
        ...state,
        messages: [
          ...state.messages,
          { kind: 'user', messageId: action.messageId, content: action.content },
        ],
      };

    case 'clear_messages':
      return { ...state, messages: [] };

    default:
      return state;
  }
}

/**
 * Store durable-operation progress and mark the end of an Agent turn.
 *
 * Operation status is authoritative over the stream: a tool may have
 * succeeded while the provider is still producing the next turn.  Rendering
 * an explicit terminal marker keeps that boundary visible in a long-running
 * transcript and prevents a previous tool row from looking like live work.
 */
function updateOperation(state: CliState, operation: any): CliState {
  const operations = { ...state.operations, [operation.operation_id]: operation };
  const terminal = operation.status === 'succeeded' || operation.status === 'failed' || operation.status === 'cancelled';
  if (!terminal) return { ...state, operations, activeOperationId: operation.operation_id };

  // This outer operation only confirms prompt delivery. The Agent Run has its
  // own lifecycle and may still be running or waiting for approval.
  if (operation.kind === 'agent_message') {
    return { ...state, operations, activeOperationId: undefined };
  }

  // HTTP operation polling and the websocket event stream are independent.
  // If the terminal poll wins the race, do not leave a visually impossible
  // "running" tool beside the completed marker; retain it as explicitly
  // uncertain until a later snapshot can reconcile the final tool result.
  const messagesWithSettledTools = state.messages.map((item) =>
    item.kind === 'tool' && (item.status === 'running' || item.status === 'proposed')
      ? {
          ...item,
          status: 'uncertain' as const,
          summary: item.summary ?? '工具事件收尾中',
        }
      : item,
  );

  const marker = `operation:${operation.operation_id}:${operation.status}`;
  const alreadyRendered = messagesWithSettledTools.some(
    (item) => item.kind === 'system' && item.id === marker,
  );
  if (alreadyRendered) return { ...state, operations, activeOperationId: operation.operation_id };

  const label = operation.status === 'succeeded'
    ? '本轮 Agent 已完成'
    : operation.status === 'cancelled'
      ? '本轮 Agent 已取消'
      : '本轮 Agent 失败';
  const detail = operation.error_message || operation.progress_summary;
  return {
    ...state,
    operations,
    activeOperationId: operation.operation_id,
    messages: [
      ...messagesWithSettledTools,
      {
        kind: 'system',
        id: marker,
        content: `${label}${detail ? ` · ${String(detail).slice(0, 300)}` : ''}`,
        timestamp: new Date(),
      },
    ],
  };
}

/**
 * Handle a stream event.
 */
function handleStreamEvent(state: CliState, event: any): CliState {
  // Reject old sequence numbers
  if (event.sequence <= state.stream.lastSequence) {
    return state;
  }

  // Update sequence
  const newStream: StreamStatus = {
    ...state.stream,
    lastSequence: event.sequence,
  };

  // Process event based on type
  switch (event.event_type) {
    case 'agent.text.delta':
      return {
        ...state,
        stream: newStream,
        messages: mergeTextDelta(state.messages, event),
      };

    case 'agent.message.completed':
      return {
        ...state,
        stream: newStream,
        messages: finalizeMessage(state.messages, event),
      };

    case 'tool.proposed':
    case 'tool_call.started':
    case 'tool_call.completed':
    case 'tool_call.status_changed':
      return {
        ...state,
        stream: newStream,
        messages: updateToolStatus(
          state.messages,
          event,
          Boolean(state.activeOperationId && ['succeeded', 'failed', 'cancelled'].includes(state.operations[state.activeOperationId]?.status)),
        ),
      };

    case 'todo.changed': {
      const items = Array.isArray(event.items) ? event.items : [];
      return {
        ...state,
        stream: newStream,
        todos: items
          .filter(
            (item: any) =>
              item && typeof item.todo_id === 'string' && typeof item.content === 'string',
          )
          .map((item: any) => ({
            todoId: item.todo_id,
            content: item.content,
            status:
              item.status === 'completed'
                ? 'completed'
                : item.status === 'in_progress'
                  ? 'in_progress'
                  : 'pending',
          })),
      };
    }

    case 'agent_run.status_changed':
      if (event.status === 'waiting_approval') {
        return {
          ...state,
          stream: newStream,
          messages: upsertRunStatus(
            state.messages,
            event.run_id,
            'Agent 已暂停，等待审批',
          ),
        };
      }
      return { ...state, stream: newStream };

    case 'agent_run.completed':
      return {
        ...state,
        stream: newStream,
        activity: { kind: 'idle' },
        messages: upsertRunStatus(state.messages, event.run_id, '调查完成'),
        todos: state.todos.map((todo) => ({ ...todo, status: 'completed' as const })),
      };

    case 'agent_run.failed': {
      const reason = friendlyFailureReason(event.reason_preview);
      return {
        ...state,
        stream: newStream,
        activity: { kind: 'idle' },
        messages: upsertRunStatus(
          state.messages,
          event.run_id ?? event.investigation_id ?? 'investigation',
          `调查失败${reason ? `：${reason}` : ''}`,
        ),
      };
    }

    case 'investigation.failed':
      return {
        ...state,
        stream: newStream,
        activity: { kind: 'idle' },
        messages: state.messages.some(
          (item) => item.kind === 'system' && item.content.startsWith('调查失败'),
        )
          ? state.messages
          : upsertRunStatus(
              state.messages,
              event.investigation_id ?? 'investigation',
              '调查失败',
            ),
      };

    case 'model_round.started':
      return {
        ...state,
        stream: newStream,
        activity: {
          kind: 'model',
          round: Number(event.round_number ?? state.usage.rounds + 1),
          startedAt: typeof event.occurred_at === 'string' ? event.occurred_at : new Date().toISOString(),
        },
      };

    case 'model_round.completed':
      return {
        ...state,
        stream: newStream,
        activity: { kind: 'idle' },
        usage: {
          rounds: state.usage.rounds + 1,
          inputTokens: state.usage.inputTokens + Number(event.input_tokens ?? 0),
          outputTokens: state.usage.outputTokens + Number(event.output_tokens ?? 0),
        },
      };

    case 'approval.requested':
      return {
        ...state,
        stream: newStream,
        approvals: {
          ...state.approvals,
          // Keep the event's identifier in the same shape consumed by the
          // renderer.  The previous `{ id, status }` placeholder could never
          // satisfy the `decision_status === 'pending'` guard in App, so an
          // approval request was silently reduced to a status line with no
          // actionable card.  The synchronizer will replace this partial view
          // with the authoritative detail snapshot when available.
          [event.approval_id]: {
            approval_id: event.approval_id,
            status: 'pending',
            decision_status: 'pending',
            intent_summary: '等待服务器返回审批详情…',
            risk: 'approval_required',
            preview: '审批详情同步中',
            impact: null,
            diff: null,
            verification: null,
            rollback: null,
            downstream_status: 'pending',
          } as any,
        },
      };

    default:
      // Unknown event - advance cursor without UI mutation
      return { ...state, stream: newStream };
  }
}

function friendlyFailureReason(reason: unknown): string | undefined {
  if (typeof reason !== 'string' || reason.trim().length === 0) return undefined;
  if (/结构化调查回合无效|JSON|Expecting .+ delimiter/i.test(reason)) {
    return '模型返回格式无效，已停止本轮调查';
  }
  return reason.replace(/\s+/g, ' ').trim().slice(0, 180);
}

/**
 * Merge a text delta into existing messages.
 */
function mergeTextDelta(
  messages: readonly ConversationItem[],
  event: any
): readonly ConversationItem[] {
  const { message_id, block_id } = event;
  // The wire event uses `text`; older fixtures use `delta`.
  const delta = typeof event.delta === 'string' ? event.delta : typeof event.text === 'string' ? event.text : '';

  // Find existing block
  const existingIndex = messages.findIndex(
    (m) => m.kind === 'text' && m.messageId === message_id && m.blockId === block_id
  );

  if (existingIndex >= 0) {
    const existing = messages[existingIndex] as TextBlock;
    const updated: TextBlock = {
      ...existing,
      content: existing.content + delta,
    };

    return [
      ...messages.slice(0, existingIndex),
      updated,
      ...messages.slice(existingIndex + 1),
    ];
  }

  // New block
  const newBlock: TextBlock = {
    kind: 'text',
    messageId: message_id,
    blockId: block_id,
    content: delta,
    finalized: false,
  };

  return [...messages, newBlock];
}

/**
 * Finalize a message block.
 */
function finalizeMessage(
  messages: readonly ConversationItem[],
  event: any
): readonly ConversationItem[] {
  const { message_id } = event;

  return messages.map((m) => {
    if (m.kind === 'text' && m.messageId === message_id) {
      return { ...m, finalized: true };
    }
    return m;
  });
}

/**
 * Update tool status in messages.
 */
function updateToolStatus(
  messages: readonly ConversationItem[],
  event: any,
  operationAlreadyTerminal = false,
): readonly ConversationItem[] {
  const toolId = event.tool_id ?? event.tool_call_id;
  const { event_type } = event;
  if (typeof toolId !== 'string' || toolId.length === 0) {
    return messages;
  }

  // Map event type to tool status
  const statusMap: Record<string, ToolBlock['status']> = {
    'tool.proposed': 'proposed',
    'tool_call.started': 'running',
    'tool_call.completed': 'succeeded',
    'tool_call.status_changed': event.status === 'failed'
      ? 'failed'
      : event.status === 'succeeded'
        ? 'succeeded'
        : event.status === 'waiting_approval'
          ? 'waiting_approval'
          : event.status === 'uncertain'
            ? 'uncertain'
            : 'running',
  };

  const newStatus = operationAlreadyTerminal && (event_type === 'tool.proposed' || event_type === 'tool_call.started')
    ? 'uncertain'
    : statusMap[event_type];
  if (!newStatus) {
    return messages;
  }

  // Find existing tool block
  const existingIndex = messages.findIndex(
    (m) => m.kind === 'tool' && m.toolId === toolId
  );

  if (existingIndex >= 0) {
    const existing = messages[existingIndex] as ToolBlock;
    const updated: ToolBlock = {
      ...existing,
      status: newStatus,
      error: event.error,
      summary: event.summary ?? event.result ?? existing.summary,
    };

    return [
      ...messages.slice(0, existingIndex),
      updated,
      ...messages.slice(existingIndex + 1),
    ];
  }

  // New tool block
  const newBlock: ToolBlock = {
    kind: 'tool',
    toolId,
    toolName: event.tool_name || 'unknown',
    status: newStatus,
    error: event.error,
    summary: event.summary ?? event.result,
  };

  return [...messages, newBlock];
}

function upsertRunStatus(
  messages: readonly ConversationItem[],
  runId: unknown,
  content: string,
): readonly ConversationItem[] {
  const id = `agent-run:${String(runId ?? 'current')}:status`;
  const withoutPrevious = messages.filter(
    (item) => item.kind !== 'system' || item.id !== id,
  );
  return [...withoutPrevious, { kind: 'system', id, content, timestamp: new Date() }];
}

/**
 * Handle gap snapshot - replace projection.
 */
function handleGapSnapshot(
  state: CliState,
  snapshot: {
    messages: ConversationItem[];
    tools?: ToolEventSnapshot[];
    todos?: CliState['todos'];
    operations: Record<string, any>;
    approvals: Record<string, any>;
    sequence: number;
  }
): CliState {
  return {
    ...state,
    messages: [
      ...snapshot.messages,
      ...(snapshot.tools ?? []).map((tool): ToolBlock => ({
        kind: 'tool',
        ...tool,
      })),
    ],
    operations: snapshot.operations,
    approvals: snapshot.approvals,
    todos: snapshot.todos ?? [],
    stream: {
      ...state.stream,
      lastSequence: snapshot.sequence,
    },
  };
}
