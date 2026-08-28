/**
 * Session picker for IncidentLens CLI.
 *
 * Keyboard-driven overlay that lists remote Agent sessions and lets the
 * user select one. Selection only mutates the active session + persisted
 * lastSessionId; it never creates or resumes anything.
 *
 * Keys: ↑/↓ to move, Enter to select, esc to cancel.
 */

import React, { useEffect, useRef, useState } from 'react';
import { Box, Text, useInput } from 'ink';
import type { AgentSessionView } from '@incidentlens/protocol';

export interface SessionPickerProps {
  readonly sessions: readonly AgentSessionView[];
  readonly onSelect: (session: AgentSessionView) => void;
  readonly onCancel: () => void;
  readonly focused?: boolean;
}

/**
 * Session picker overlay.
 */
export function SessionPicker({
  sessions,
  onSelect,
  onCancel,
  focused = true,
}: SessionPickerProps): React.ReactElement {
  const [selectedIndex, setSelectedIndex] = useState(0);

  // Latest-value ref so the useInput handler never reads a stale index.
  const selectedRef = useRef<number>(selectedIndex);
  selectedRef.current = selectedIndex;

  // Keep the selection inside bounds when the list changes.
  useEffect(() => {
    if (selectedRef.current >= sessions.length && sessions.length > 0) {
      setSelectedIndex(sessions.length - 1);
    }
  }, [sessions.length]);

  useInput(
    (input, key) => {
      if (!focused) {
        return;
      }

      if (key.escape) {
        onCancel();
        return;
      }

      if (key.return) {
        const session = sessions[selectedRef.current];
        if (session) {
          onSelect(session);
        }
        return;
      }

      if (key.upArrow || input === 'k') {
        setSelectedIndex((current) => Math.max(0, current - 1));
        return;
      }

      if (key.downArrow || input === 'j') {
        setSelectedIndex((current) => Math.min(sessions.length - 1, current + 1));
        return;
      }
    },
    { isActive: focused },
  );

  if (sessions.length === 0) {
    return (
      <Box flexDirection="column">
        <Text bold color="blue">
          Sessions
        </Text>
        <Text color="gray">No sessions found. Create one with /new.</Text>
      </Box>
    );
  }

  return (
    <Box flexDirection="column">
      <Text bold color="blue">
        Sessions
      </Text>
      {sessions.map((session, index) => {
        const isSelected = index === selectedIndex;
        return (
          <Box key={session.session_id} paddingLeft={2}>
            <Text color={isSelected ? 'blue' : undefined} inverse={isSelected}>
              {isSelected ? '>' : ' '}
              {` ${session.title ?? 'untitled'}`}
            </Text>
            <Text color="gray">{`  ${session.session_id}`}</Text>
            {session.target_id && <Text color="gray">{`  target:${session.target_id}`}</Text>}
            {session.status && <Text color="gray">{`  [${session.status}]`}</Text>}
          </Box>
        );
      })}
      <Box marginTop={1}>
        <Text color="gray">↑/↓ to move, Enter to select, esc to cancel</Text>
      </Box>
    </Box>
  );
}