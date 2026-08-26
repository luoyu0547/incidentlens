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
import { SessionController } from '../features/sessions/session-controller.js';
import {
  createSessionCommands,
  type SessionCommandRuntime,
} from '../features/sessions/session-commands.js';
import type { AgentSessionView } from '@incidentlens/protocol';
import { Conversation } from '../ui/Conversation.js';
import { PromptInput } from '../ui/PromptInput.js';
import { StatusLine } from '../ui/StatusLine.js';
import { CommandPalette } from '../ui/CommandPalette.js';
import { TargetWizard, RemoveTargetPrompt } from '../ui/TargetWizard.js';
import { SessionPicker } from '../ui/SessionPicker.js';
import { ApprovalCard } from '../ui/ApprovalCard.js';
import { ApprovalReasonPrompt } from '../ui/ApprovalReasonPrompt.js';
import { ApprovalControllerImpl } from '../features/approvals/approval-controller.js';
import { createApprovalCommands, type ApprovalCommandRuntime } from '../features/approvals/approval-commands.js';
import { SessionSynchronizer } from '../stream/session-synchronizer.js';

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

  // Sessions loaded into the picker overlay.
  const [pickerSessions, setPickerSessions] = useState<readonly AgentSessionView[]>([]);
  const [pickerError, setPickerError] = useState<string | undefined>(undefined);

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

  // Session controller shared by session commands and natural-language
  // submits. It implements no-target blocking, create-on-first-message,
  // and durable-operation tracking that mirrors into state.operations.
  const sessionController = useMemo(
    () =>
      new SessionController({
        api: deps.api,
        configStore: deps.configStore,
        profileName: 'default',
        dispatch,
        onOperationProgress: (operation) =>
          dispatch({ type: 'update_operation', operation }),
      }),
    [deps.api, deps.configStore, dispatch]
  );

  // Approval decisions always refresh and render the server response.
  const approvalController = useMemo(() => new ApprovalControllerImpl({ api: deps.api }), [deps.api]);
  const [approvalPrompt, setApprovalPrompt] = useState<{ id: string; decision: 'approve' | 'reject' }>();
  const approvalRuntime = useMemo<ApprovalCommandRuntime>(() => ({
    controller: approvalController,
    listPending: async () => (await deps.api.listApprovals({ status: 'pending', limit: 500 })).items,
    getCurrentApprovalId: () => Object.values(stateRef.current.approvals).find((a) => a.decision_status === 'pending')?.approval_id,
    openReasonPrompt: (id, decision) => setApprovalPrompt({ id, decision }),
    showDiff: (approval) => dispatch({ type: 'system_message', content: approval.diff ?? 'No safe diff available.', timestamp: deps.now() }),
  }), [approvalController, deps, dispatch]);

  // Keep the session controller's view of the active target/session in
  // sync with the reducer on every render.
  sessionController.sync(state.target, state.session);

  // A stream is opened only after bootstrap has produced an authoritative
  // session snapshot. Aborting this controller is a clean client shutdown;
  // it never issues a server cancellation.
  const synchronizerRef = useRef<SessionSynchronizer | undefined>(undefined);
  useEffect(() => {
    if (state.bootstrap !== 'ready' || !state.session) {
      return;
    }
    const synchronizer = new SessionSynchronizer({
      api: deps.api,
      configStore: deps.configStore,
      profileName: 'default',
      eventStream: deps.eventStream as never,
      host: {
        dispatch,
        onCursorAdvance: () => {
          // Cursor persistence is owned by the synchronizer and follows
          // reducer dispatch for every accepted event.
        },
      },
    });
    synchronizerRef.current = synchronizer;
    const controller = new AbortController();
    void deps.configStore.load('default').then((profile) => {
      if (controller.signal.aborted) return;
      synchronizer.setInitialCursor(
        state.session!.session_id,
        profile?.lastSequenceBySession[state.session!.session_id] ?? state.stream.lastSequence,
      );
      void synchronizer.start(controller.signal);
    });
    return () => {
      controller.abort();
      synchronizerRef.current = undefined;
    };
  }, [state.bootstrap, state.session?.session_id, deps, dispatch]);

  // Load sessions when the picker opens.
  useEffect(() => {
    if (state.overlay.kind === 'session-picker') {
      void sessionController
        .list()
        .then((sessions) => setPickerSessions(sessions))
        .catch((error) => {
          const message = error instanceof Error ? error.message : 'Failed to load sessions';
          setPickerError(message);
        });
    }
  }, [state.overlay.kind, sessionController]);

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

  // Runtime callbacks backing the /session command group.
  const sessionRuntime = useMemo<SessionCommandRuntime>(
    () => ({
      controller: sessionController,
      openPicker: () =>
        dispatch({ type: 'set_overlay', overlay: { kind: 'session-picker' } }),
      status: (text) => dispatch({ type: 'system_message', content: text, timestamp: deps.now() }),
      error: (text) =>
        dispatch({
          type: 'system_message',
          content: `Error: ${text}`,
          timestamp: deps.now(),
        }),
    }),
    [sessionController, dispatch, deps]
  );

  // Command registry populated with the /target and /session command groups.
  const registry = useMemo(
    () =>
      createCommandRegistry(
        [...createTargetCommands(targetRuntime), ...createSessionCommands(sessionRuntime), ...createApprovalCommands(approvalRuntime)],
        commandContext,
      ),
    [targetRuntime, sessionRuntime, approvalRuntime, commandContext]
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
          // Send as natural language message through the session
          // controller (no-target blocking + create-on-first-message).
          void sessionController
            .sendNaturalLanguage(value)
            .catch((error) => {
              const text = error instanceof Error ? error.message : 'Failed to send message';
              dispatch({
                type: 'system_message',
                content: `Error: ${text}`,
                timestamp: deps.now(),
              });
            });
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
    [dispatch, registry, commandContext, intoMessages, sessionController, deps]
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
  const sessionPickerOpen = state.overlay.kind === 'session-picker';

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
      {Object.values(state.approvals).filter((approval) => approval.decision_status === 'pending').map((approval) => (
        <ApprovalCard
          key={approval.approval_id}
          approval={approval}
          focused={state.input.focused}
          promptEmpty={state.input.value.length === 0}
          overlayActive={state.overlay.kind !== 'none' || approvalPrompt !== undefined}
          onAction={(action) => action === 'diff'
            ? approvalRuntime.showDiff(approval)
            : approvalRuntime.openReasonPrompt(approval.approval_id, action)}
        />
      ))}
      {approvalPrompt && (
        <ApprovalReasonPrompt
          decision={approvalPrompt.decision}
          onCancel={() => setApprovalPrompt(undefined)}
          onSubmit={(reason) => {
            const prompt = approvalPrompt;
            setApprovalPrompt(undefined);
            void approvalController.decide(prompt.id, prompt.decision, reason).then((updated) => {
              dispatch({ type: 'set_approval', approval: updated });
            }).catch((error) => dispatch({ type: 'system_message', content: `Error: ${error instanceof Error ? error.message : 'Approval decision failed'}`, timestamp: deps.now() }));
          }}
        />
      )}

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

      {/* Session Picker Overlay */}
      {sessionPickerOpen &&
        (pickerError ? (
          <Box>
            <Text color="red">Error loading sessions: {pickerError}</Text>
          </Box>
        ) : (
          <SessionPicker
            sessions={pickerSessions}
            onSelect={(session) => {
              void sessionController.select(session).catch((error) => {
                const message = error instanceof Error ? error.message : 'Failed to select session';
                dispatch({
                  type: 'system_message',
                  content: `Error: ${message}`,
                  timestamp: deps.now(),
                });
              });
              dispatch({
                type: 'system_message',
                content: `Selected session ${session.session_id} (${session.title ?? 'untitled'})`,
                timestamp: deps.now(),
              });
              setPickerSessions([]);
              setPickerError(undefined);
              dispatch({ type: 'set_overlay', overlay: { kind: 'none' } });
            }}
            onCancel={() => {
              setPickerSessions([]);
              setPickerError(undefined);
              dispatch({ type: 'set_overlay', overlay: { kind: 'none' } });
            }}
            focused={true}
          />
        ))}

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
