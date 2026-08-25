/**
 * Main App component for IncidentLens CLI.
 *
 * Renders the single-column conversation shell with:
 * - IncidentLens header
 * - Current target/session status
 * - Conversation history
 * - Status line
 * - Prompt input
 */

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { Box, Text, useInput, useApp } from 'ink';
import type { AppDependencies } from './dependencies.js';
import { bootstrap } from './bootstrap.js';
import type { CliState, CliAction } from '../state/cli-state.js';
import { createInitialState, reducer } from '../state/reducer.js';
import { parseInput } from '../commands/parser.js';
import { Conversation } from '../ui/Conversation.js';
import { PromptInput } from '../ui/PromptInput.js';
import { StatusLine } from '../ui/StatusLine.js';
import { CommandPalette } from '../ui/CommandPalette.js';

interface AppProps {
  readonly dependencies: AppDependencies;
  readonly initialState?: Partial<CliState>;
}

/**
 * Main App component.
 */
export function App({ dependencies: deps, initialState }: AppProps): React.ReactElement {
  const [state, setState] = useState<CliState>(() => ({
    ...createInitialState(),
    ...initialState,
  }));

  const { exit } = useApp();

  // Dispatch function for state updates
  const dispatch = useCallback((action: CliAction) => {
    setState((prev) => reducer(prev, action));
  }, []);

  // Bootstrap on mount
  useEffect(() => {
    if (state.bootstrap === 'loading') {
      void bootstrap(deps, dispatch);
    }
  }, [state.bootstrap, deps, dispatch]);

  // Handle keyboard input
  useInput((input, key) => {
    // Handle Ctrl+C
    if (key.ctrl && input === 'c') {
      if (state.overlay.kind !== 'none') {
        // Close overlay
        dispatch({ type: 'set_overlay', overlay: { kind: 'none' } });
      } else if (state.input.value.length > 0) {
        // Clear input
        dispatch({ type: 'set_input', input: { value: '' } });
      } else {
        // Exit
        deps.exit();
        exit();
      }
      return;
    }

    // Handle Escape
    if (key.escape) {
      if (state.overlay.kind !== 'none') {
        dispatch({ type: 'set_overlay', overlay: { kind: 'none' } });
      }
      return;
    }
  });

  // Handle input submission
  const handleSubmit = useCallback(
    (value: string) => {
      if (value.trim() === '') {
        return;
      }

      const parsed = parseInput(value);

      switch (parsed.kind) {
        case 'empty':
          // Do nothing
          break;

        case 'message':
          // Send as natural language message
          if (state.session) {
            void deps.api.sendMessage(state.session.id, { content: value }, {
              idempotencyKey: crypto.randomUUID(),
            });
          }
          break;

        case 'command':
          // Handle command (to be implemented in later tasks)
          break;

        case 'incomplete-command':
          // Show command palette
          dispatch({
            type: 'set_overlay',
            overlay: { kind: 'command-palette', query: parsed.query },
          });
          break;
      }

      // Clear input
      dispatch({ type: 'set_input', input: { value: '' } });
    },
    [state.session, deps.api, dispatch]
  );

  // Handle input change
  const handleInputChange = useCallback(
    (value: string) => {
      dispatch({ type: 'set_input', input: { value } });
    },
    [dispatch]
  );

  // Show loading state
  if (state.bootstrap === 'loading') {
    return (
      <Box flexDirection="column">
        <Text bold color="blue">
          IncidentLens
        </Text>
        <Text color="gray">Loading...</Text>
      </Box>
    );
  }

  // Show authentication required
  if (state.bootstrap === 'authentication-required') {
    return (
      <Box flexDirection="column">
        <Text bold color="blue">
          IncidentLens
        </Text>
        <Text color="yellow">Authentication required</Text>
        <Text color="gray">Please set INCIDENTLENS_TOKEN environment variable</Text>
      </Box>
    );
  }

  // Show incompatible
  if (state.bootstrap === 'incompatible') {
    return (
      <Box flexDirection="column">
        <Text bold color="blue">
          IncidentLens
        </Text>
        <Text color="red">Incompatible version</Text>
      </Box>
    );
  }

  return (
    <Box flexDirection="column">
      {/* Header */}
      <Box>
        <Text bold color="blue">
          IncidentLens
        </Text>
        <Text color="gray"> | </Text>
        <Text>{state.target?.name ?? 'No target'}</Text>
        {state.session && (
          <>
            <Text color="gray"> | </Text>
            <Text>{state.session.title}</Text>
          </>
        )}
      </Box>

      {/* Conversation */}
      <Conversation messages={state.messages} />

      {/* Status Line */}
      <StatusLine
        streamConnected={state.stream.connected}
        pendingApprovals={Object.values(state.approvals).filter((a) => a.status === 'pending').length}
      />

      {/* Command Palette Overlay */}
      {state.overlay.kind === 'command-palette' && (
        <CommandPalette
          query={state.overlay.query}
          commands={[]}
          selectedIndex={0}
          onSelect={() => {}}
          onCancel={() => dispatch({ type: 'set_overlay', overlay: { kind: 'none' } })}
          focused={true}
        />
      )}

      {/* Prompt Input */}
      {state.overlay.kind === 'none' && (
        <PromptInput
          value={state.input.value}
          onChange={handleInputChange}
          onSubmit={handleSubmit}
          focused={state.input.focused}
        />
      )}
    </Box>
  );
}
