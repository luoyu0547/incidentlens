/**
 * Prompt input component for IncidentLens CLI.
 *
 * Renders the input prompt for user messages.
 */

import React from 'react';
import { Box, Text } from 'ink';

interface PromptInputProps {
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly onSubmit: (value: string) => void;
  readonly focused: boolean;
  readonly placeholder?: string;
}

/**
 * Prompt input component.
 */
export function PromptInput({
  value,
  onChange: _onChange,
  onSubmit: _onSubmit,
  focused,
  placeholder = 'Type a message or / for commands',
}: PromptInputProps): React.ReactElement {
  return (
    <Box
      marginTop={1}
      paddingX={1}
      borderStyle="round"
      borderColor={focused ? 'magenta' : 'gray'}
      flexDirection="row"
    >
      <Text color={focused ? 'magenta' : 'gray'} bold>
        {'❯ '}
      </Text>
      {value.length > 0 ? (
        <Text color="white">{value}{focused ? '▌' : ''}</Text>
      ) : (
        <Text color="gray">{focused ? '▌ ' : ''}{placeholder}</Text>
      )}
    </Box>
  );
}
