/**
 * Slash command types for IncidentLens CLI.
 *
 * Commands are the primary navigation mechanism in the CLI.
 * They must be discoverable, typed, and context-aware.
 */

import type { TargetView, AgentSessionView } from '@incidentlens/protocol';

/**
 * Bootstrap state affects command availability.
 */
export type BootstrapState =
  | 'loading'
  | 'ready'
  | 'authentication-required'
  | 'incompatible';

/**
 * Command context provides current application state for command availability checks.
 */
export interface CommandContext {
  readonly target: TargetView | undefined;
  readonly session: AgentSessionView | undefined;
  readonly bootstrap: BootstrapState;
  readonly capabilities: Set<string>;
}

/**
 * Parsed slash command invocation.
 */
export interface CommandInvocation {
  readonly path: readonly string[];
  readonly args: string;
}

/**
 * Command execution result.
 */
export type CommandResult =
  | { kind: 'noop' }
  | { kind: 'message'; text: string }
  | { kind: 'navigate'; target: string }
  | { kind: 'error'; message: string };

/**
 * Slash command definition.
 */
export interface SlashCommand {
  readonly path: readonly string[];
  readonly summary: string;
  readonly group:
    | 'help'
    | 'target'
    | 'connection'
    | 'session'
    | 'scope'
    | 'investigation'
    | 'approval'
    | 'system';
  readonly usage: string;
  readonly dangerous: boolean;
  available(context: CommandContext): boolean;
  execute(invocation: CommandInvocation, context: CommandContext): Promise<CommandResult>;
}

/**
 * Parsed input from user.
 */
export type ParsedInput =
  | { kind: 'empty' }
  | { kind: 'message'; text: string }
  | { kind: 'command'; invocation: CommandInvocation }
  | { kind: 'incomplete-command'; query: string };
