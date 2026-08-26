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
      return { ...state, session: action.session };

    case 'set_approval':
      return {
        ...state,
        approvals: { ...state.approvals, [action.approval.approval_id]: action.approval },
      };

    case 'update_operation':
      return {
        ...state,
        operations: { ...state.operations, [action.operation.operation_id]: action.operation },
      };

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

    case 'clear_messages':
      return { ...state, messages: [] };

    default:
      return state;
  }
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
        messages: updateToolStatus(state.messages, event),
      };

    case 'approval.requested':
      return {
        ...state,
        stream: newStream,
        approvals: {
          ...state.approvals,
          [event.approval_id]: { id: event.approval_id, status: 'pending' } as any,
        },
      };

    default:
      // Unknown event - advance cursor without UI mutation
      return { ...state, stream: newStream };
  }
}

/**
 * Merge a text delta into existing messages.
 */
function mergeTextDelta(
  messages: readonly ConversationItem[],
  event: any
): readonly ConversationItem[] {
  const { message_id, block_id, delta } = event;

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
  event: any
): readonly ConversationItem[] {
  const { tool_id, event_type } = event;

  // Map event type to tool status
  const statusMap: Record<string, ToolBlock['status']> = {
    'tool.proposed': 'proposed',
    'tool_call.started': 'running',
    'tool_call.completed': 'succeeded',
    'tool_call.status_changed': event.status === 'failed' ? 'failed' : 'running',
  };

  const newStatus = statusMap[event_type];
  if (!newStatus) {
    return messages;
  }

  // Find existing tool block
  const existingIndex = messages.findIndex(
    (m) => m.kind === 'tool' && m.toolId === tool_id
  );

  if (existingIndex >= 0) {
    const existing = messages[existingIndex] as ToolBlock;
    const updated: ToolBlock = {
      ...existing,
      status: newStatus,
      error: event.error,
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
    toolId: tool_id,
    toolName: event.tool_name || 'unknown',
    status: newStatus,
    error: event.error,
  };

  return [...messages, newBlock];
}

/**
 * Handle gap snapshot - replace projection.
 */
function handleGapSnapshot(
  state: CliState,
  snapshot: {
    messages: ConversationItem[];
    operations: Record<string, any>;
    approvals: Record<string, any>;
    sequence: number;
  }
): CliState {
  return {
    ...state,
    messages: snapshot.messages,
    operations: snapshot.operations,
    approvals: snapshot.approvals,
    stream: {
      ...state.stream,
      lastSequence: snapshot.sequence,
    },
  };
}
