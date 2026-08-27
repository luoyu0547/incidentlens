import React, { useState } from 'react';
import { Box, Text, useInput } from 'ink';
import type { ApprovalDetailView } from '@incidentlens/protocol';

export interface ApprovalCardProps {
  readonly approval: ApprovalDetailView;
  readonly focused: boolean;
  readonly promptEmpty: boolean;
  readonly overlayActive: boolean;
  readonly onAction: (action: 'approve' | 'reject' | 'approve_all' | 'diff') => void;
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
  const [selected, setSelected] = useState(0);
  const options = [
    ['yes', '允许本次'],
    ['no', '拒绝本次'],
    ['yes all', '允许本 session 后续审批'],
  ] as const;
  useInput((_input, key) => {
    if (!hotkeysActive) return;
    if (key.upArrow) setSelected((value) => (value + options.length - 1) % options.length);
    else if (key.downArrow) setSelected((value) => (value + 1) % options.length);
    else if (key.return) onAction(selected === 0 ? 'approve' : selected === 1 ? 'reject' : 'approve_all');
  }, { isActive: hotkeysActive });

  return <Box flexDirection="column" marginTop={1} marginBottom={1} borderStyle="round" borderColor={focused ? 'yellow' : 'gray'} paddingX={1}>
    <Text bold color="yellow">需要审批 · {approval.kind} · {approval.approval_id}</Text>
    <Text color="gray">操作</Text><Text>{safeApprovalText(approval.intent_summary) ?? '未提供'}</Text>
    {safeApprovalText(approval.preview) && <Text>命令: {safeApprovalText(approval.preview)}</Text>}
    <Text color="gray">风险: {approval.risk} · 截止: {approval.expires_at}</Text>
    {safeApprovalText(approval.diff) && <Text>Diff: {safeApprovalText(approval.diff)}</Text>}
    {safeApprovalText(approval.impact) && <Text>Impact: {safeApprovalText(approval.impact)}</Text>}
    {safeApprovalText(approval.verification) && <Text>Verification: {safeApprovalText(approval.verification)}</Text>}
    {safeApprovalText(approval.rollback) && <Text>Rollback: {safeApprovalText(approval.rollback)}</Text>}
    <Text color="gray">状态: <Text color={approval.decision_status === 'pending' ? 'yellow' : 'green'}>{approval.decision_status}</Text> · 下游: {approval.downstream_status}</Text>
    {hotkeysActive && <>
      <Text color="cyan">↑/↓ 选择，Enter 确认</Text>
      {options.map(([command, label], index) => <Text key={command} color={index === selected ? 'cyan' : 'gray'}>{index === selected ? '›' : ' '} {command} — {label}</Text>)}
    </>}
  </Box>;
}
