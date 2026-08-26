/**
 * Main App component for IncidentLens CLI.
 *
 * Renders the single-column conversation shell with:
 * - IncidentLens header
 * - Current target/session status
 * - Conversation history
 * - Status line
 * - Prompt input
 * - Overlays: command palette, target wizard, typed remove confirmation
 */

import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { Box, Text, useInput, useApp } from 'ink';
import type { AppDependencies } from './dependencies.js';
import { bootstrap } from './bootstrap.js';
import type { CliState, CliAction } from '../state/cli-state.js';
import { createInitialState, reducer } from '../state/reducer.js';
import { parseInput } from '../commands/parser.js';
import { executeCommand } from '../commands/execute-command.js';
import type { CommandContext, CommandResult } from '../commands/types.js';
import { createCommandRegistry } from '../commands/registry.js';
import { TargetController } from '../features/targets/target-controller.js';
import {
  createTargetCommands,
  type TargetCommandRuntime,
} from '../features/targets/target-commands.js';
import { Conversation } from '../ui/Conversation.js';
import { PromptInput } from '../ui/PromptInput.js';
import { StatusLine } from '../ui/StatusLine.js';
import { CommandPalette } from '../ui/CommandPalette.js';
import { TargetWizard, RemoveTargetPrompt } from '../ui/TargetWizard.js';

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

  // Latest state ref so stable runtime callbacks never read stale state.
  const stateRef = useRef<CliState>(state);
  stateRef.current = state;

  // Target controller shared by commands and the wizard.
  const controller = useMemo(
    () =>
      new TargetController({
        api: deps.api,
        configStore: deps.configStore,
        profileName: 'default',
        dispatch,
      }),
    [deps.api, deps.configStore, dispatch]
  );

  // Context used for command availability checks.
  const commandContext: CommandContext = useMemo(
    () => ({
      target: state.target,
      session: state.session,
      bootstrap: state.bootstrap,
      capabilities: new Set<string>(),
    }),
    [state.target, state.session, state.bootstrap]
  );

  // Runtime callbacks backing the /target command group.
  const targetRuntime = useMemo<TargetCommandRuntime>(
    () => ({
      controller,
      openWizard: (mode, target) =>
        dispatch({
          type: 'set_overlay',
          overlay: { kind: 'target-wizard', mode, target, step: 'name' },
        }),
      openRemoveConfirmation: (target) =>
        dispatch({
          type: 'set_overlay',
          overlay: {
            kind: 'confirmation',
            target,
            onConfirm: () => {
              void controller
                .remove(target.target_id)
                .then(() => {
                  if (stateRef.current.target?.target_id === target.target_id) {
                    dispatch({ type: 'clear_target' });
                  }
                  dispatch({
                    type: 'system_message',
                    content: `Removed target ${target.name}`,
                    timestamp: deps.now(),
                  });
                })
                .catch((error) => {
                  const message =
                    error instanceof Error ? error.message : 'Failed to remove target';
                  dispatch({
                    type: 'system_message',
                    content: `Error: ${message}`,
                    timestamp: deps.now(),
                  });
                });
            },
          },
        }),
      status: (text) => dispatch({ type: 'system_message', content: text, timestamp: deps.now() }),
      error: (text) =>
        dispatch({
          type: 'system_message',
          content: `Error: ${text}`,
          timestamp: deps.now(),
        }),
    }),
    [controller, dispatch, deps]
  );

  // Command registry populated with the /target command group.
  const registry = useMemo(
    () => createCommandRegistry(createTargetCommands(targetRuntime), commandContext),
    [targetRuntime, commandContext]
  );

  // Surface a CommandResult as a system message in the conversation.
  const intoMessages = useCallback(
    (result: CommandResult) => {
      if (result.kind === 'message') {
        dispatch({ type: 'system_message', content: result.text, timestamp: deps.now() });
      } else if (result.kind === 'error') {
        dispatch({
          type: 'system_message',
          content: `Error: ${result.message}`,
          timestamp: deps.now(),
        });
      } else if (result.kind === 'navigate') {
        dispatch({
          type: 'system_message',
          content: `Navigating to ${result.target}…`,
          timestamp: deps.now(),
        });
      }
    },
    [dispatch, deps]
  );

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
            void deps.api.sendMessage(
              state.session.session_id,
              { content: value },
              {
                idempotencyKey: crypto.randomUUID(),
              }
            );
          }
          break;

        case 'command':
          // Route through the command registry.
          void executeCommand(parsed.invocation, registry, commandContext).then(intoMessages);
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
    [state.session, deps.api, dispatch, registry, commandContext, intoMessages]
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

  // Narrowed overlay variants so closures can reference them safely.
  const wizardOverlay = state.overlay.kind === 'target-wizard' ? state.overlay : undefined;
  const confirmationOverlay = state.overlay.kind === 'confirmation' ? state.overlay : undefined;

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
        pendingApprovals={
          Object.values(state.approvals).filter((a) => a.status === 'pending').length
        }
      />

      {/* Command Palette Overlay */}
      {state.overlay.kind === 'command-palette' && (
        <CommandPalette
          query={state.overlay.query}
          commands={registry.getAvailable(commandContext)}
          selectedIndex={0}
          onSelect={(cmd) => {
            void executeCommand({ path: [...cmd.path], args: '' }, registry, commandContext).then(
              intoMessages
            );
            dispatch({ type: 'set_overlay', overlay: { kind: 'none' } });
          }}
          onCancel={() => dispatch({ type: 'set_overlay', overlay: { kind: 'none' } })}
          focused={true}
        />
      )}

      {/* Target Wizard Overlay */}
      {wizardOverlay && (
        <TargetWizard
          mode={wizardOverlay.mode}
          target={wizardOverlay.target}
          controller={controller}
          onComplete={(target) => {
            dispatch({ type: 'set_target', target });
            dispatch({ type: 'set_overlay', overlay: { kind: 'none' } });
            dispatch({
              type: 'system_message',
              content: `Saved target ${target.name}`,
              timestamp: deps.now(),
            });
          }}
          onCancel={() => dispatch({ type: 'set_overlay', overlay: { kind: 'none' } })}
        />
      )}

      {/* Typed Remove Confirmation Overlay */}
      {confirmationOverlay && (
        <RemoveTargetPrompt
          target={confirmationOverlay.target}
          onConfirm={() => {
            confirmationOverlay.onConfirm();
            dispatch({ type: 'set_overlay', overlay: { kind: 'none' } });
          }}
          onCancel={() => dispatch({ type: 'set_overlay', overlay: { kind: 'none' } })}
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
