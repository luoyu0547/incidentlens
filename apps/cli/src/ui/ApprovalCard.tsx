import React from 'react';
import { Box, Text, useInput } from 'ink';
import type { ApprovalDetailView } from '@incidentlens/protocol';

export interface ApprovalCardProps {
  readonly approval: ApprovalDetailView;
  readonly focused: boolean;
  readonly promptEmpty: boolean;
  readonly overlayActive: boolean;
  readonly onAction: (action: 'approve' | 'reject' | 'diff') => void;
}

export function approvalHotkeysActive(
  focused: boolean,
  promptEmpty: boolean,
  overlayActive: boolean,
): boolean {
  return focused && promptEmpty && !overlayActive;
}

export function safeApprovalText(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  const redacted = value.replace(/(token|password|secret|authorization|credential)\s*[:=]\s*[^\s,;]+/gi, '$1=[redacted]');
  return redacted.length > 240 ? `${redacted.slice(0, 237)}...` : redacted;
}

export function ApprovalCard({ approval, focused, promptEmpty, overlayActive, onAction }: ApprovalCardProps): React.ReactElement {
  const hotkeysActive = approvalHotkeysActive(focused, promptEmpty, overlayActive);
  useInput((input) => {
    if (!hotkeysActive) return;
    const action = input.toLowerCase() === 'a' ? 'approve' : input.toLowerCase() === 'r' ? 'reject' : input.toLowerCase() === 'd' ? 'diff' : undefined;
    if (action) onAction(action);
  }, { isActive: hotkeysActive });

  return <Box flexDirection="column" borderStyle="round" borderColor={focused ? 'yellow' : 'gray'} paddingX={1}>
    <Text bold>Approval {approval.approval_id}</Text>
    <Text>Intent: {approval.intent_summary}</Text>
    <Text>Risk: {approval.risk}  Expires: {approval.expires_at}</Text>
    {safeApprovalText(approval.diff) && <Text>Diff: {safeApprovalText(approval.diff)}</Text>}
    {safeApprovalText(approval.impact) && <Text>Impact: {safeApprovalText(approval.impact)}</Text>}
    {safeApprovalText(approval.verification) && <Text>Verification: {safeApprovalText(approval.verification)}</Text>}
    {safeApprovalText(approval.rollback) && <Text>Rollback: {safeApprovalText(approval.rollback)}</Text>}
    <Text>Decision persisted: {approval.decision_status}</Text>
    <Text>Downstream: {approval.downstream_status}</Text>
    {hotkeysActive && <Text color="gray">A approve, R reject, D diff</Text>}
  </Box>;
}
