import { describe, expect, it } from 'vitest';
import { createInitialState, reducer } from './reducer.js';
import type { CliState, ConversationItem } from './cli-state.js';
import type { OperationView } from '@incidentlens/protocol';

function makeOperation(overrides: Partial<OperationView> = {}): OperationView {
  return {
    operation_id: 'op-1',
    kind: 'agent_message',
    target_id: 'target-1',
    session_id: 'session-1',
    investigation_id: null,
    status: 'running',
    progress_summary: null,
    error_code: null,
    error_message: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    finished_at: null,
    ...overrides,
  };
}

describe('reducer', () => {
  describe('text delta merge', () => {
    it('merges text deltas by message and block ID', () => {
      const state = createInitialState();

      const event1 = {
        event_type: 'agent.text.delta' as const,
        session_id: 'sess-1',
        sequence: 1,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        message_id: 'msg-1',
        block_id: 'block-1',
        delta: 'Hello',
      };

      const event2 = {
        event_type: 'agent.text.delta' as const,
        session_id: 'sess-1',
        sequence: 2,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        message_id: 'msg-1',
        block_id: 'block-1',
        delta: ' World',
      };

      const state1 = reducer(state, { type: 'stream_event', event: event1 });
      const state2 = reducer(state1, { type: 'stream_event', event: event2 });

      expect(state2.messages).toHaveLength(1);
      expect(state2.messages[0]).toEqual({
        kind: 'text',
        messageId: 'msg-1',
        blockId: 'block-1',
        content: 'Hello World',
        finalized: false,
      });
    });

    it('separates deltas by block ID', () => {
      const state = createInitialState();

      const event1 = {
        event_type: 'agent.text.delta' as const,
        session_id: 'sess-1',
        sequence: 1,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        message_id: 'msg-1',
        block_id: 'block-1',
        delta: 'First',
      };

      const event2 = {
        event_type: 'agent.text.delta' as const,
        session_id: 'sess-1',
        sequence: 2,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        message_id: 'msg-1',
        block_id: 'block-2',
        delta: 'Second',
      };

      const state1 = reducer(state, { type: 'stream_event', event: event1 });
      const state2 = reducer(state1, { type: 'stream_event', event: event2 });

      expect(state2.messages).toHaveLength(2);
    });
  });

  describe('finalization', () => {
    it('finalizes a message block', () => {
      const state = createInitialState();

      const deltaEvent = {
        event_type: 'agent.text.delta' as const,
        session_id: 'sess-1',
        sequence: 1,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        message_id: 'msg-1',
        block_id: 'block-1',
        delta: 'Content',
      };

      const finalizeEvent = {
        event_type: 'agent.message.completed' as const,
        session_id: 'sess-1',
        sequence: 2,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        message_id: 'msg-1',
      };

      const state1 = reducer(state, { type: 'stream_event', event: deltaEvent });
      const state2 = reducer(state1, { type: 'stream_event', event: finalizeEvent });

      expect(state2.messages[0]).toEqual({
        kind: 'text',
        messageId: 'msg-1',
        blockId: 'block-1',
        content: 'Content',
        finalized: true,
      });
    });
  });

  describe('duplicate and old sequence rejection', () => {
    it('rejects duplicate events', () => {
      const state = createInitialState();

      const event = {
        event_type: 'agent.text.delta' as const,
        session_id: 'sess-1',
        sequence: 1,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        message_id: 'msg-1',
        block_id: 'block-1',
        delta: 'Content',
      };

      const state1 = reducer(state, { type: 'stream_event', event });
      const state2 = reducer(state1, { type: 'stream_event', event });

      // Should not duplicate
      expect(state2.messages).toHaveLength(1);
    });

    it('rejects old sequence numbers', () => {
      const state = createInitialState();

      const event1 = {
        event_type: 'agent.text.delta' as const,
        session_id: 'sess-1',
        sequence: 5,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        message_id: 'msg-1',
        block_id: 'block-1',
        delta: 'First',
      };

      const event2 = {
        event_type: 'agent.text.delta' as const,
        session_id: 'sess-1',
        sequence: 3,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        message_id: 'msg-1',
        block_id: 'block-1',
        delta: 'Old',
      };

      const state1 = reducer(state, { type: 'stream_event', event: event1 });
      const state2 = reducer(state1, { type: 'stream_event', event: event2 });

      expect(state2.messages[0]).toEqual({
        kind: 'text',
        messageId: 'msg-1',
        blockId: 'block-1',
        content: 'First',
        finalized: false,
      });
    });
  });

  describe('tool transitions', () => {
    it('handles tool proposed event', () => {
      const state = createInitialState();

      const event = {
        event_type: 'tool.proposed' as const,
        session_id: 'sess-1',
        sequence: 1,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        tool_id: 'tool-1',
        tool_name: 'search_logs',
      };

      const newState = reducer(state, { type: 'stream_event', event });

      expect(newState.messages).toHaveLength(1);
      expect(newState.messages[0]).toEqual({
        kind: 'tool',
        toolId: 'tool-1',
        toolName: 'search_logs',
        status: 'proposed',
      });
    });

    it('accepts the backend tool_call_id field', () => {
      const state = createInitialState();
      const newState = reducer(state, {
        type: 'stream_event',
        event: {
          event_type: 'tool.proposed',
          sequence: 1,
          tool_call_id: 'call-1',
          tool_name: 'registry_info',
          arguments_preview: '{}',
        },
      });

      expect(newState.messages[0]).toMatchObject({
        kind: 'tool',
        toolId: 'call-1',
        toolName: 'registry_info',
        status: 'proposed',
      });
      expect(newState.messages[0]).toHaveProperty('summary', undefined);
    });

    it('projects waiting approval without marking the tool complete', () => {
      const proposed = reducer(createInitialState(), {
        type: 'stream_event',
        event: {
          event_type: 'tool.proposed',
          sequence: 1,
          tool_call_id: 'call-1',
          tool_name: 'shell_exec',
          summary: '执行受控命令 uptime',
        },
      });
      const waiting = reducer(proposed, {
        type: 'stream_event',
        event: {
          event_type: 'tool_call.status_changed',
          sequence: 2,
          tool_call_id: 'call-1',
          status: 'waiting_approval',
        },
      });

      expect(waiting.messages[0]).toMatchObject({ status: 'waiting_approval' });
    });

    it('handles tool running event', () => {
      const state = createInitialState();

      const proposedEvent = {
        event_type: 'tool.proposed' as const,
        session_id: 'sess-1',
        sequence: 1,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        tool_id: 'tool-1',
        tool_name: 'search_logs',
      };

      const runningEvent = {
        event_type: 'tool_call.started' as const,
        session_id: 'sess-1',
        sequence: 2,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        tool_id: 'tool-1',
      };

      const state1 = reducer(state, { type: 'stream_event', event: proposedEvent });
      const state2 = reducer(state1, { type: 'stream_event', event: runningEvent });

      expect(state2.messages[0]).toEqual({
        kind: 'tool',
        toolId: 'tool-1',
        toolName: 'search_logs',
        status: 'running',
      });
    });

    it('handles tool succeeded event', () => {
      const state = createInitialState();

      const proposedEvent = {
        event_type: 'tool.proposed' as const,
        session_id: 'sess-1',
        sequence: 1,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        tool_id: 'tool-1',
        tool_name: 'search_logs',
      };

      const succeededEvent = {
        event_type: 'tool_call.completed' as const,
        session_id: 'sess-1',
        sequence: 2,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        tool_id: 'tool-1',
      };

      const state1 = reducer(state, { type: 'stream_event', event: proposedEvent });
      const state2 = reducer(state1, { type: 'stream_event', event: succeededEvent });

      expect(state2.messages[0]).toEqual({
        kind: 'tool',
        toolId: 'tool-1',
        toolName: 'search_logs',
        status: 'succeeded',
      });
    });

    it('handles tool failed event', () => {
      const state = createInitialState();

      const proposedEvent = {
        event_type: 'tool.proposed' as const,
        session_id: 'sess-1',
        sequence: 1,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        tool_id: 'tool-1',
        tool_name: 'search_logs',
      };

      const failedEvent = {
        event_type: 'tool_call.status_changed' as const,
        session_id: 'sess-1',
        sequence: 2,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        tool_id: 'tool-1',
        status: 'failed',
        error: 'Timeout',
      };

      const state1 = reducer(state, { type: 'stream_event', event: proposedEvent });
      const state2 = reducer(state1, { type: 'stream_event', event: failedEvent });

      expect(state2.messages[0]).toEqual({
        kind: 'tool',
        toolId: 'tool-1',
        toolName: 'search_logs',
        status: 'failed',
        error: 'Timeout',
      });
    });
  });

  describe('approval transitions', () => {
    it('handles approval requested event', () => {
      const state = createInitialState();

      const event = {
        event_type: 'approval.requested' as const,
        session_id: 'sess-1',
        sequence: 1,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        approval_id: 'approval-1',
      };

      const newState = reducer(state, { type: 'stream_event', event });

      expect(newState.approvals['approval-1']).toBeDefined();
    });
  });

  describe('todo projection', () => {
    it('projects todo.changed into persistent plan state', () => {
      const state = reducer(createInitialState(), {
        type: 'stream_event',
        event: {
          event_type: 'todo.changed',
          sequence: 1,
          items: [
            { todo_id: 't1', content: '检查负载', status: 'in_progress' },
            { todo_id: 't2', content: '检查磁盘', status: 'pending' },
          ],
        },
      });

      expect(state.todos).toEqual([
        { todoId: 't1', content: '检查负载', status: 'in_progress' },
        { todoId: 't2', content: '检查磁盘', status: 'pending' },
      ]);
    });
  });

  describe('usage projection', () => {
    it('accumulates model round token usage', () => {
      const first = reducer(createInitialState(), {
        type: 'stream_event',
        event: {
          event_type: 'model_round.completed',
          sequence: 1,
          input_tokens: 1200,
          output_tokens: 300,
        },
      });
      const second = reducer(first, {
        type: 'stream_event',
        event: {
          event_type: 'model_round.completed',
          sequence: 2,
          input_tokens: 800,
          output_tokens: 200,
        },
      });

      expect(second.usage).toEqual({ rounds: 2, inputTokens: 2000, outputTokens: 500 });
    });
  });

  describe('agent activity projection', () => {
    it('shows a running model round and clears it on completion', () => {
      const running = reducer(createInitialState(), {
        type: 'stream_event',
        event: {
          event_type: 'model_round.started',
          sequence: 1,
          round_number: 2,
          occurred_at: '2026-08-27T01:00:00Z',
        },
      });
      expect(running.activity).toEqual({
        kind: 'model',
        round: 2,
        startedAt: '2026-08-27T01:00:00Z',
      });

      const completed = reducer(running, {
        type: 'stream_event',
        event: { event_type: 'model_round.completed', sequence: 2 },
      });
      expect(completed.activity).toEqual({ kind: 'idle' });
    });

    it('renders a safe provider-format failure and clears activity', () => {
      const running = {
        ...createInitialState(),
        activity: { kind: 'model' as const, round: 2, startedAt: '2026-08-27T01:00:00Z' },
      };
      const failed = reducer(running, {
        type: 'stream_event',
        event: {
          event_type: 'agent_run.failed',
          sequence: 1,
          run_id: 'run-1',
          reason_preview: "OpenAI-compatible API 返回的结构化调查回合无效：Expecting ',' delimiter",
        },
      });
      expect(failed.activity).toEqual({ kind: 'idle' });
      expect(failed.messages.at(-1)).toMatchObject({
        kind: 'system',
        content: '调查失败：模型返回格式无效，已停止本轮调查',
      });
    });
  });

  describe('unknown events', () => {
    it('advances cursor without UI mutation', () => {
      const state = createInitialState();

      const event = {
        event_type: 'unknown.event.type' as any,
        session_id: 'sess-1',
        sequence: 1,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
      };

      const newState = reducer(state, { type: 'stream_event', event });

      // Should advance sequence but not add messages
      expect(newState.messages).toHaveLength(0);
    });
  });

  describe('update_operation', () => {
    it('stores operations keyed by operation_id', () => {
      const state = createInitialState();
      const operation = makeOperation({ status: 'running' });

      const newState = reducer(state, { type: 'update_operation', operation });

      expect(newState.operations['op-1']).toEqual(operation);
    });

    it('replaces an existing operation with a newer snapshot', () => {
      const state = createInitialState();
      const running = makeOperation({ status: 'running' });
      const succeeded = makeOperation({ status: 'succeeded', progress_summary: 'done' });

      const state1 = reducer(state, { type: 'update_operation', operation: running });
      const state2 = reducer(state1, { type: 'update_operation', operation: succeeded });

      expect(state2.operations['op-1']).toEqual(succeeded);
      expect(Object.keys(state2.operations)).toHaveLength(1);
    });
  });

  describe('gap snapshot', () => {
    it('replaces projection on gap', () => {
      const state = createInitialState();

      const action = {
        type: 'gap_snapshot' as const,
        snapshot: {
          messages: [],
          operations: {},
          approvals: {},
          sequence: 10,
        },
      };

      const newState = reducer(state, action);

      expect(newState.stream.lastSequence).toBe(10);
    });
  });

  describe('no raw payload retention', () => {
    it('does not store raw event data', () => {
      const state = createInitialState();

      const event = {
        event_type: 'agent.text.delta' as const,
        session_id: 'sess-1',
        sequence: 1,
        schema_version: 1 as const,
        occurred_at: new Date().toISOString(),
        message_id: 'msg-1',
        block_id: 'block-1',
        delta: 'Content',
        raw_payload: { some: 'data' },
      };

      const newState = reducer(state, { type: 'stream_event', event });

      // State should not contain raw_payload
      expect(newState).not.toHaveProperty('raw_payload');
    });
  });
});

describe('createInitialState', () => {
  it('creates valid initial state', () => {
    const state = createInitialState();

    expect(state.bootstrap).toBe('loading');
    expect(state.target).toBeUndefined();
    expect(state.session).toBeUndefined();
    expect(state.messages).toEqual([]);
    expect(state.operations).toEqual({});
    expect(state.approvals).toEqual({});
    expect(state.todos).toEqual([]);
    expect(state.usage).toEqual({ rounds: 0, inputTokens: 0, outputTokens: 0 });
    expect(state.stream.connected).toBe(false);
    expect(state.stream.lastSequence).toBe(0);
  });
});
