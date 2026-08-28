/**
 * Deterministic input parser for IncidentLens CLI.
 *
 * Converts raw user input into typed ParsedInput:
 * - Empty/whitespace → empty
 * - Slash prefix → command or incomplete-command
 * - Everything else → message
 */

import type { ParsedInput } from './types.js';

// Known command prefixes that can have subcommands
const COMMAND_PREFIXES = new Set([
  'target',
  'approve',
  'reject',
]);

// Known single-word commands
const SINGLE_WORD_COMMANDS = new Set([
  'help',
  'status',
  'new',
  'sessions',
  'resume',
  'rename',
  'clear',
  'cancel',
  'approvals',
  'diff',
  'reconnect',
  'exit',
]);

// Known subcommands for each prefix
const SUBCOMMANDS: Record<string, Set<string>> = {
  target: new Set(['add', 'edit', 'test', 'remove']),
  approve: new Set(),
  reject: new Set(),
};

/**
 * Parse raw user input into a typed representation.
 *
 * Rules:
 * - Empty or whitespace-only → empty
 * - Starts with '/' → command or incomplete-command
 * - Everything else → message (including unknown slash commands)
 */
export function parseInput(raw: string): ParsedInput {
  const trimmed = raw.trim();

  // Empty input
  if (trimmed === '') {
    return { kind: 'empty' };
  }

  // Not a slash command - treat as message
  if (!trimmed.startsWith('/')) {
    return { kind: 'message', text: raw };
  }

  // Strip leading slash
  const withoutSlash = trimmed.slice(1);

  // Just '/' or '/ ' → incomplete command with empty query
  if (withoutSlash === '' || withoutSlash === ' ') {
    return { kind: 'incomplete-command', query: '' };
  }

  // Find the first word (up to first space)
  const spaceIndex = withoutSlash.indexOf(' ');
  const firstWord = spaceIndex === -1 ? withoutSlash : withoutSlash.slice(0, spaceIndex);
  const rest = spaceIndex === -1 ? '' : withoutSlash.slice(spaceIndex + 1);

  // Check if first word is a known single-word command
  if (SINGLE_WORD_COMMANDS.has(firstWord)) {
    return {
      kind: 'command',
      invocation: { path: [firstWord], args: rest },
    };
  }

  // Check if first word is a known prefix
  if (COMMAND_PREFIXES.has(firstWord)) {
    // Check if we have a subcommand
    if (rest.length > 0) {
      const restSpaceIndex = rest.indexOf(' ');
      const secondWord = restSpaceIndex === -1 ? rest : rest.slice(0, restSpaceIndex);
      const remainingArgs = restSpaceIndex === -1 ? '' : rest.slice(restSpaceIndex + 1);

      const validSubcommands = SUBCOMMANDS[firstWord];

      if (validSubcommands && validSubcommands.has(secondWord)) {
        // Valid subcommand - path includes both words
        return {
          kind: 'command',
          invocation: {
            path: [firstWord, secondWord],
            args: remainingArgs,
          },
        };
      }

      // Unknown subcommand - treat entire rest as args
      return {
        kind: 'command',
        invocation: { path: [firstWord], args: rest },
      };
    }

    // Just the prefix with no subcommand
    return {
      kind: 'command',
      invocation: { path: [firstWord], args: '' },
    };
  }

  // Check if first word could be a prefix of a known command
  // This handles cases like /tar which is prefix of /target
  for (const prefix of Array.from(COMMAND_PREFIXES)) {
    if (prefix.startsWith(firstWord) && firstWord.length < prefix.length) {
      return { kind: 'incomplete-command', query: firstWord };
    }
  }

  // Check if first word is a prefix of a known single-word command
  for (const cmd of Array.from(SINGLE_WORD_COMMANDS)) {
    if (cmd.startsWith(firstWord) && firstWord.length < cmd.length) {
      return { kind: 'incomplete-command', query: firstWord };
    }
  }

  // Unknown command - treat as message
  return { kind: 'message', text: raw };
}

/**
 * Normalize input for display (trim, collapse whitespace).
 */
export function normalizeInput(raw: string): string {
  return raw.trim().replace(/\s+/g, ' ');
}
