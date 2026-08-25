/**
 * Command registry for IncidentLens CLI.
 *
 * Manages command registration, lookup, and context-aware filtering.
 * Commands are registered once and looked up by path prefix.
 */

import type { SlashCommand, CommandContext } from './types.js';

/**
 * Registry of available slash commands.
 */
export interface CommandRegistry {
  readonly commands: readonly SlashCommand[];
  find(path: readonly string[]): SlashCommand | undefined;
  getAvailable(context: CommandContext): readonly SlashCommand[];
  getByGroup(): Map<string, readonly SlashCommand[]>;
}

/**
 * Create a command registry from a list of commands.
 *
 * @param commands - Commands to register
 * @param context - Initial context for filtering
 */
export function createCommandRegistry(
  commands: readonly SlashCommand[],
  _context: CommandContext
): CommandRegistry {
  // Deduplicate by path (last wins)
  const commandMap = new Map<string, SlashCommand>();

  for (const cmd of commands) {
    const key = cmd.path.join('/');
    commandMap.set(key, cmd);
  }

  const uniqueCommands = Array.from(commandMap.values());

  return {
    commands: uniqueCommands,

    find(path: readonly string[]): SlashCommand | undefined {
      // Try exact match first
      const exactKey = path.join('/');
      const exact = uniqueCommands.find((cmd) => cmd.path.join('/') === exactKey);
      if (exact) {
        return exact;
      }

      // Try longest prefix match
      let bestMatch: SlashCommand | undefined;
      let bestLength = 0;

      for (const cmd of uniqueCommands) {
        if (cmd.path.length <= path.length) {
          const cmdKey = cmd.path.join('/');
          const pathPrefix = path.slice(0, cmd.path.length).join('/');

          if (cmdKey === pathPrefix && cmd.path.length > bestLength) {
            bestMatch = cmd;
            bestLength = cmd.path.length;
          }
        }
      }

      return bestMatch;
    },

    getAvailable(context: CommandContext): readonly SlashCommand[] {
      return uniqueCommands.filter((cmd) => cmd.available(context));
    },

    getByGroup(): Map<string, readonly SlashCommand[]> {
      const groups = new Map<string, SlashCommand[]>();

      for (const cmd of uniqueCommands) {
        const group = groups.get(cmd.group) ?? [];
        group.push(cmd);
        groups.set(cmd.group, group);
      }

      return groups;
    },
  };
}
