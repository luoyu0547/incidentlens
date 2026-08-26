/**
 * CLI state types for IncidentLens.
 *
 * Defines the shape of the application state that drives the UI.
 * This is a pure projection of server events and HTTP snapshots.
 */

import type { TargetView, AgentSessionView, OperationView, ApprovalDetailView } from '@incidentlens/protocol';

/**
 * Bootstrap state affects UI rendering and command availability.
 */
export type BootstrapState =
  | 'loading'
  | 'ready'
  | 'authentication-required'
  | 'incompatible';

/**
 * Stream connection status.
 */
export interface StreamStatus {
  readonly connected: boolean;
  readonly lastSequence: number;
  readonly error?: string;
}

/**
 * Input state for the prompt.
 */
export interface InputState {
  readonly focused: boolean;
  readonly value: string;
}

/**
 * Overlay state for modals and wizards.
 */
export type OverlayState =
  | { kind: 'none' }
  | { kind: 'command-palette'; query: string }
  | {
      kind: 'target-wizard';
      mode: 'create' | 'edit';
      target?: TargetView;
      step: string;
    }
  | { kind: 'confirmation'; target: TargetView; onConfirm: () => void }
  | { kind: 'session-picker' };

/**
 * Conversation item - safe UI projection of messages and tool updates.
 */
export type ConversationItem =
  | TextBlock
  | ToolBlock
  | ApprovalBlock
  | SystemMessage;

/**
 * Text block from agent response.
 */
export interface TextBlock {
  readonly kind: 'text';
  readonly messageId: string;
  readonly blockId: string;
  readonly content: string;
  readonly finalized?: boolean;
}

/**
 * Tool execution block.
 */
export interface ToolBlock {
  readonly kind: 'tool';
  readonly toolId: string;
  readonly toolName: string;
  readonly status: 'proposed' | 'running' | 'succeeded' | 'failed' | 'uncertain';
  readonly error?: string;
  readonly summary?: string;
}

/**
 * Approval request block.
 */
export interface ApprovalBlock {
  readonly kind: 'approval';
  readonly approvalId: string;
  readonly status: 'pending' | 'approved' | 'rejected' | 'expired';
}

/**
 * System message (e.g., status updates, errors).
 */
export interface SystemMessage {
  readonly kind: 'system';
  readonly content: string;
  readonly timestamp: Date;
}

/**
 * Complete CLI application state.
 */
export interface CliState {
  readonly bootstrap: BootstrapState;
  readonly target?: TargetView;
  readonly session?: AgentSessionView;
  readonly messages: readonly ConversationItem[];
  readonly operations: Readonly<Record<string, OperationView>>;
  readonly approvals: Readonly<Record<string, ApprovalDetailView>>;
  readonly stream: StreamStatus;
  readonly input: InputState;
  readonly overlay: OverlayState;
}

/**
 * Actions that can modify the state.
 */
export type CliAction =
  | { type: 'bootstrap_complete'; state: BootstrapState }
  | { type: 'set_target'; target: TargetView }
  | { type: 'clear_target' }
  | { type: 'set_session'; session: AgentSessionView }
  | { type: 'stream_event'; event: any }
  | { type: 'gap_snapshot'; snapshot: { messages: ConversationItem[]; operations: Record<string, OperationView>; approvals: Record<string, ApprovalDetailView>; sequence: number } }
  | { type: 'set_stream_status'; status: Partial<StreamStatus> }
  | { type: 'set_input'; input: Partial<InputState> }
  | { type: 'set_overlay'; overlay: OverlayState }
  | { type: 'system_message'; content: string; timestamp: Date }
  | { type: 'clear_messages' };
