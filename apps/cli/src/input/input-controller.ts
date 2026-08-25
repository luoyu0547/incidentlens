/**
 * Input controller for IncidentLens CLI.
 *
 * Manages input state and routing between command palette and message input.
 */

import type { CliState, CliAction } from '../state/cli-state.js';
import { parseInput } from '../commands/parser.js';

/**
 * Handle keyboard input and route to appropriate handler.
 */
export function handleInput(
  state: CliState,
  input: string,
  key: { ctrl?: boolean; escape?: boolean; return?: boolean },
  dispatch: (action: CliAction) => void
): void {
  // Handle Ctrl+C
  if (key.ctrl && input === 'c') {
    if (state.overlay.kind !== 'none') {
      // Close overlay
      dispatch({ type: 'set_overlay', overlay: { kind: 'none' } });
    } else if (state.input.value.length > 0) {
      // Clear input
      dispatch({ type: 'set_input', input: { value: '' } });
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

  // Handle Enter
  if (key.return) {
    handleSubmit(state, dispatch);
    return;
  }

  // Handle regular input
  const newValue = state.input.value + input;
  dispatch({ type: 'set_input', input: { value: newValue } });
}

/**
 * Handle input submission.
 */
function handleSubmit(
  state: CliState,
  dispatch: (action: CliAction) => void
): void {
  const value = state.input.value;

  if (value.trim() === '') {
    return;
  }

  const parsed = parseInput(value);

  switch (parsed.kind) {
    case 'empty':
      // Do nothing
      break;

    case 'message':
      // Message handling will be done by the caller
      break;

    case 'command':
      // Command handling will be done by the caller
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
}
