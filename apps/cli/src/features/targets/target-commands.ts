/**
 * Target slash commands for IncidentLens CLI.
 *
 * Registers the `/target` command group on the existing SlashCommand
 * pipeline:
 *   /target            list targets
 *   /target <name>     select a target by name
 *   /target add        open the target configuration wizard (create)
 *   /target edit       open the target configuration wizard (edit)
 *   /target test       run connectivity + host-key verification
 *   /target remove     remove the current target (typed confirmation)
 *
 * Safety rules implemented here:
 * - The wizard sends metadata and an opaque `authentication_ref` only;
 *   private-key material is never collected or forwarded.
 * - `/target test` follows the returned Operation and reports only the
 *   server-redacted result (verified host-key source/fingerprint or a
 *   safe failure). Raw SSH output is never displayed.
 * - Removal is gated behind an explicit typed confirmation overlay.
 */

import type { SlashCommand } from '../../commands/types.js';
import type { TargetView } from '@incidentlens/protocol';
import { TargetController, trackTargetTest } from './target-controller.js';

/**
 * Runtime callbacks the command layer needs from the host shell.
 * Keeps commands framework-agnostic and unit-testable.
 */
export interface TargetCommandRuntime {
  readonly controller: TargetController;
  /** Open the create/edit wizard overlay. */
  readonly openWizard: (mode: 'create' | 'edit', target?: TargetView) => void;
  /** Open the typed deletion confirmation overlay. */
  readonly openRemoveConfirmation: (target: TargetView) => void;
  /** Push a safe status message into the conversation. */
  readonly status: (text: string) => void;
  /** Push a safe error message into the conversation. */
  readonly error: (text: string) => void;
}

/**
 * Build the `/target` command group.
 */
export function createTargetCommands(runtime: TargetCommandRuntime): SlashCommand[] {
  return [
    {
      path: ['target'],
      summary: 'List or select a target',
      group: 'target',
      usage: '/target [name]',
      dangerous: false,
      available: (ctx) => ctx.bootstrap === 'ready',
      execute: async (invocation) => {
        const name = invocation.args.trim();

        if (name === '') {
          const targets = await runtime.controller.list();
          if (targets.length === 0) {
            return {
              kind: 'message',
              text: 'No targets configured yet. Use /target add to create one.',
            };
          }
          const lines = targets
            .map((t) => `- ${t.name}  ${t.ssh_user}@${t.host}:${t.ssh_port}`)
            .join('\n');
          return {
            kind: 'message',
            text: `Targets:\n${lines}\nSelect one with /target <name>.`,
          };
        }

        const targets = await runtime.controller.list();
        const match = targets.find((t) => t.name === name);
        if (!match) {
          return { kind: 'error', message: `Target not found: ${name}` };
        }

        await runtime.controller.select(match);
        return { kind: 'message', text: `Selected target ${match.name} (${match.host})` };
      },
    },
    {
      path: ['target', 'add'],
      summary: 'Add a target with the wizard',
      group: 'target',
      usage: '/target add',
      dangerous: false,
      available: (ctx) => ctx.bootstrap === 'ready',
      execute: async () => {
        runtime.openWizard('create');
        return { kind: 'message', text: 'Opening target wizard…' };
      },
    },
    {
      path: ['target', 'edit'],
      summary: 'Edit the current target',
      group: 'target',
      usage: '/target edit',
      dangerous: false,
      available: (ctx) => ctx.bootstrap === 'ready' && ctx.target !== undefined,
      execute: async (_invocation, ctx) => {
        const target = ctx.target;
        if (!target) {
          return { kind: 'error', message: 'No target selected. Use /target <name> first.' };
        }
        runtime.openWizard('edit', target);
        return { kind: 'message', text: 'Opening target editor…' };
      },
    },
    {
      path: ['target', 'test'],
      summary: 'Test connectivity and host key',
      group: 'target',
      usage: '/target test',
      dangerous: false,
      available: (ctx) => ctx.bootstrap === 'ready' && ctx.target !== undefined,
      execute: async (_invocation, ctx) => {
        const target = ctx.target;
        if (!target) {
          return { kind: 'error', message: 'No target selected. Use /target <name> first.' };
        }

        const accepted = await runtime.controller.test(target.target_id);

        // Track the returned Operation and report only the safe result.
        // Errors from getOperation (network/server failures) are caught
        // inside trackTargetTest and surfaced via onError → runtime.error.
        void trackTargetTest(
          (operationId, signal) => runtime.controller.getOperation(operationId, signal),
          accepted.operation_id,
          (progress) => {
            if (progress.status === 'succeeded') {
              runtime.status(
                `Target verified: ${progress.summary ?? 'host key and connectivity OK'}`
              );
            } else if (progress.status === 'failed') {
              runtime.error(
                `Target test failed: ${progress.error ?? 'verification failed safely'}`
              );
            } else {
              runtime.status(`Target test ${progress.status} (operation ${accepted.operation_id})`);
            }
          },
          { onError: (message) => runtime.error(`Target test error: ${message}`) }
        );

        return {
          kind: 'message',
          text: `Target test started for ${target.name} (operation ${accepted.operation_id}). Results will be reported.`,
        };
      },
    },
    {
      path: ['target', 'remove'],
      summary: 'Remove the current target',
      group: 'target',
      usage: '/target remove',
      dangerous: true,
      available: (ctx) => ctx.bootstrap === 'ready' && ctx.target !== undefined,
      execute: async (_invocation, ctx) => {
        const target = ctx.target;
        if (!target) {
          return { kind: 'error', message: 'No target selected. Use /target <name> first.' };
        }
        runtime.openRemoveConfirmation(target);
        return {
          kind: 'message',
          text: `Removing ${target.name} requires typed confirmation.`,
        };
      },
    },
  ];
}
