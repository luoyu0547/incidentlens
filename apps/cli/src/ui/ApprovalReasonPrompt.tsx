import React, { useRef, useState } from 'react';
import { Box, Text, useInput } from 'ink';

export interface ApprovalReasonPromptProps {
  readonly decision: 'approve' | 'reject';
  readonly onSubmit: (reason: string) => void;
  readonly onCancel: () => void;
}

/** Explicit reason capture. Empty or whitespace-only reasons cannot submit. */
export function ApprovalReasonPrompt({ decision, onSubmit, onCancel }: ApprovalReasonPromptProps): React.ReactElement {
  const [value, setValue] = useState('');
  const [error, setError] = useState<string | undefined>();
  const valueRef = useRef(value);
  valueRef.current = value;
  useInput((input, key) => {
    if (key.escape) return onCancel();
    if (key.return) {
      const reason = valueRef.current.trim();
      if (!reason) return setError('Reason is required');
      return onSubmit(reason);
    }
    if (key.backspace || key.delete) return setValue((current) => current.slice(0, -1));
    if (input.length > 0 && !key.ctrl && !key.meta) {
      setValue((current) => current + input);
      setError(undefined);
    }
  });
  return <Box flexDirection="column" borderStyle="round" borderColor="yellow" paddingX={1}>
    <Text bold>{decision === 'approve' ? 'Approve' : 'Reject'} approval</Text>
    <Text>Reason (required): {value}</Text>
    {error && <Text color="red">{error}</Text>}
    <Text color="gray">Enter to persist decision, Esc to cancel</Text>
  </Box>;
}
