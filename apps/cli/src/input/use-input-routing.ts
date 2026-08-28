/**
 * React hook for input routing in IncidentLens CLI.
 *
 * Connects keyboard input to the state reducer.
 */

import { useCallback } from 'react';
import { useInput as useInkInput } from 'ink';
import type { CliState, CliAction } from '../state/cli-state.js';

interface UseInputRoutingOptions {
  readonly state: CliState;
  readonly dispatch: (action: CliAction) => void;
  readonly onSubmit: (value: string) => void;
}

/**
 * Hook for handling keyboard input routing.
 */
export function useInputRouting({ state, dispatch, onSubmit }: UseInputRoutingOptions): void {
  useInkInput((input, key) => {
    // Handle Ctrl+C
    if (key.ctrl && input === 'c') {
      if (state.overlay.kind !== 'none') {
        dispatch({ type: 'set_overlay', overlay: { kind: 'none' } });
      } else if (state.input.value.length > 0) {
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
      if (state.input.value.trim().length > 0) {
        onSubmit(state.input.value);
        dispatch({ type: 'set_input', input: { value: '' } });
      }
      return;
    }

    // Handle Backspace
    if (key.backspace) {
      if (state.input.value.length > 0) {
        dispatch({
          type: 'set_input',
          input: { value: state.input.value.slice(0, -1) },
        });
      }
      return;
    }

    // Handle regular character input
    if (input.length > 0 && !key.ctrl && !key.meta) {
      dispatch({
        type: 'set_input',
        input: { value: state.input.value + input },
      });
    }
  });
}
