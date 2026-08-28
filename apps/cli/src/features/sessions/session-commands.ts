/**
 * Session slash commands for IncidentLens CLI.
 *
 * Registers session lifecycle commands on the SlashCommand pipeline:
 *   /new               create a new session
 *   /sessions          list all sessions
 *   /sessions <id>     select a session by id
 *   /resume <id>       resume a session (attach + request server recovery)
 *   /rename <title>    rename the active session
 *   /cancel            cancel the active session's in-flight operation
 *
 * Safety rules:
 * - `/cancel` is the ONLY path that calls the cancel API. `/exit`,
 *   Ctrl+C, stdin close, and WS disconnect never cancel server work.
 * - `/resume` attaches to a session and requests server-side recovery;
 *   it never fabricates state locally.
 * - Only server-redacted fields are displayed. Raw tool args/output,
 *   provider payloads, hidden reasoning, and canonical intents are
 *   never surfaced.
 */

import type { SlashCommand } from '../../commands/types.js';
import type { SessionController } from './session-controller.js';

/**
 * Runtime callbacks the command layer needs from the host shell.
 * Keeps commands framework-agnostic and unit-testable.
 */
export interface SessionCommandRuntime {
  readonly controller: SessionController;
  /** Push a safe status message into the conversation. */
  readonly status: (text: string) => void;
  /** Push a safe error message into the conversation. */
  readonly error: (text: string) => void;
  /** Open the session picker overlay. */
  readonly openPicker: () => void;
}

/**
 * Build session lifecycle commands.
 */
export function createSessionCommands(runtime: SessionCommandRuntime): SlashCommand[] {
  return [
    {
      path: ['new'],
      summary: 'Create a new Agent session',
      group: 'session',
      usage: '/new [title]',
      dangerous: false,
      available: (ctx) => ctx.bootstrap === 'ready' && ctx.target !== undefined,
      execute: async (invocation) => {
        const title = invocation.args.trim() || undefined;
        const session = await runtime.controller.create(title);
        runtime.status(`Created session ${session.session_id} (${session.title ?? 'untitled'})`);
        return { kind: 'message', text: `Session created: ${session.session_id}` };
      },
    },
    {
      path: ['sessions'],
      summary: 'List or select sessions',
      group: 'session',
      usage: '/sessions [id]',
      dangerous: false,
      available: (ctx) => ctx.bootstrap === 'ready',
      execute: async (invocation) => {
        const id = invocation.args.trim();

        if (id !== '') {
          // Select by session id.
          const session = await runtime.controller.get(id);
          await runtime.controller.select(session);
          runtime.status(`Selected session ${id} (${session.title ?? 'untitled'})`);
          return { kind: 'message', text: `Selected session ${id}` };
        }

        // Open the session picker overlay
        runtime.openPicker();
        return { kind: 'message', text: 'Opening session picker…' };
      },
    },
    {
      path: ['resume'],
      summary: 'Resume a session by id',
      group: 'session',
      usage: '/resume <session-id>',
      dangerous: false,
      available: (ctx) => ctx.bootstrap === 'ready',
      execute: async (invocation) => {
        const sessionId = invocation.args.trim();
        if (!sessionId) {
          return { kind: 'error', message: 'Usage: /resume <session-id>' };
        }

        const accepted = await runtime.controller.resume(sessionId);
        runtime.status(
          `Resumed session ${sessionId} (operation ${accepted.operation_id})`,
        );
        return {
          kind: 'message',
          text: `Session ${sessionId} resumed.`,
        };
      },
    },
    {
      path: ['rename'],
      summary: 'Rename the active session',
      group: 'session',
      usage: '/rename <title>',
      dangerous: false,
      available: (ctx) => ctx.bootstrap === 'ready' && ctx.session !== undefined,
      execute: async (invocation) => {
        const title = invocation.args.trim();
        if (!title) {
          return { kind: 'error', message: 'Usage: /rename <title>' };
        }

        const updated = await runtime.controller.rename(title);
        runtime.status(`Renamed session to "${updated.title ?? ''}"`);
        return { kind: 'message', text: `Session renamed to "${updated.title ?? ''}".` };
      },
    },
    {
      path: ['cancel'],
      summary: 'Cancel the active session operation',
      group: 'session',
      usage: '/cancel',
      dangerous: true,
      available: (ctx) => ctx.bootstrap === 'ready' && ctx.session !== undefined,
      execute: async () => {
        const operation = await runtime.controller.cancelCurrent();
        runtime.status(
          `Cancelled operation ${operation.operation_id} (status: ${operation.status})`,
        );
        return { kind: 'message', text: `Operation ${operation.operation_id} cancelled.` };
      },
    },
  ];
}