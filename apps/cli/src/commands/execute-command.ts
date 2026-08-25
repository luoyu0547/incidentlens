/**
 * Command executor for IncidentLens CLI.
 *
 * Routes parsed commands to their handlers and manages execution context.
 */

import type {
  SlashCommand,
  CommandInvocation,
  CommandContext,
  CommandResult,
} from './types.js';
import type { CommandRegistry } from './registry.js';

/**
 * Execute a parsed command invocation.
 *
 * @param invocation - The parsed command to execute
 * @param registry - Command registry to look up handlers
 * @param context - Current application context
 * @returns Command execution result
 */
export async function executeCommand(
  invocation: CommandInvocation,
  registry: CommandRegistry,
  context: CommandContext
): Promise<CommandResult> {
  const command = registry.find([...invocation.path]);

  if (!command) {
    return {
      kind: 'error',
      message: `Unknown command: /${invocation.path.join(' ')}`,
    };
  }

  // Check availability
  if (!command.available(context)) {
    return {
      kind: 'error',
      message: `Command not available in current context: /${invocation.path.join(' ')}`,
    };
  }

  // Check dangerous commands
  if (command.dangerous) {
    // In a real implementation, this would show a confirmation prompt
    // For now, we just execute
  }

  try {
    return await command.execute(invocation, context);
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Unknown error executing command';

    return {
      kind: 'error',
      message: `Command failed: ${message}`,
    };
  }
}

/**
 * Format a command result for display.
 */
export function formatCommandResult(result: CommandResult): string {
  switch (result.kind) {
    case 'noop':
      return '';
    case 'message':
      return result.text;
    case 'navigate':
      return `Navigating to ${result.target}...`;
    case 'error':
      return `Error: ${result.message}`;
  }
}
