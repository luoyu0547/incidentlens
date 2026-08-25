/**
 * State selectors for IncidentLens CLI.
 *
 * Pure functions that extract derived data from the application state.
 */

import type { CliState, ConversationItem, ToolBlock, TextBlock } from './cli-state.js';

/**
 * Get visible messages (non-finalized text blocks and tool blocks).
 */
export function getVisibleMessages(state: CliState): readonly ConversationItem[] {
  return state.messages.filter((m) => {
    if (m.kind === 'text') {
      // Show text blocks that have content
      return m.content.length > 0;
    }
    // Show all tool blocks
    return m.kind === 'tool';
  });
}

/**
 * Get the current target name or a placeholder.
 */
export function getTargetDisplay(state: CliState): string {
  return state.target?.name ?? 'No target selected';
}

/**
 * Get the current session title or a placeholder.
 */
export function getSessionDisplay(state: CliState): string {
  return state.session?.title ?? 'No active session';
}

/**
 * Check if there are any pending approvals.
 */
export function hasPendingApprovals(state: CliState): boolean {
  return Object.values(state.approvals).some(
    (a) => a.status === 'pending'
  );
}

/**
 * Get pending approval count.
 */
export function getPendingApprovalCount(state: CliState): number {
  return Object.values(state.approvals).filter(
    (a) => a.status === 'pending'
  ).length;
}

/**
 * Check if the stream is connected.
 */
export function isStreamConnected(state: CliState): boolean {
  return state.stream.connected;
}

/**
 * Get the last sequence number.
 */
export function getLastSequence(state: CliState): number {
  return state.stream.lastSequence;
}

/**
 * Check if bootstrap is complete.
 */
export function isBootstrapComplete(state: CliState): boolean {
  return state.bootstrap === 'ready';
}

/**
 * Get bootstrap state for display.
 */
export function getBootstrapDisplay(state: CliState): string {
  switch (state.bootstrap) {
    case 'loading':
      return 'Loading...';
    case 'ready':
      return 'Ready';
    case 'authentication-required':
      return 'Authentication required';
    case 'incompatible':
      return 'Incompatible version';
  }
}

/**
 * Get running tool blocks.
 */
export function getRunningTools(state: CliState): readonly ToolBlock[] {
  return state.messages.filter(
    (m): m is ToolBlock => m.kind === 'tool' && m.status === 'running'
  );
}

/**
 * Get the latest text block.
 */
export function getLatestTextBlock(state: CliState): TextBlock | undefined {
  for (let i = state.messages.length - 1; i >= 0; i--) {
    const m = state.messages[i];
    if (m?.kind === 'text') {
      return m;
    }
  }
  return undefined;
}
