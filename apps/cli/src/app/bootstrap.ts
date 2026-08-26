/**
 * Bootstrap sequence for IncidentLens CLI.
 *
 * Handles the initialization order:
 * profile → token → compatibility → principal → target → Session snapshot → event stream
 */

import type { AppDependencies } from './dependencies.js';
import type { CliState, CliAction } from '../state/cli-state.js';
import { assertCompatible, type ClientCompatibility } from '@incidentlens/protocol';

/**
 * Protocol versions this CLI understands. The control plane declares its
 * `minimum_cli_protocol_version` in the version response; 1.0.0 is the
 * current protocol generation served by both sides.
 */
const CLIENT_COMPATIBILITY: ClientCompatibility = {
  min_protocol_version: '1.0.0',
  max_protocol_version: '1.0.0',
};

/**
 * Bootstrap the application.
 *
 * @returns Initial state after bootstrap
 */
export async function bootstrap(
  deps: AppDependencies,
  dispatch: (action: CliAction) => void
): Promise<Partial<CliState>> {
  try {
    // 1. Load profile config
    const profile = await deps.configStore.load('default');

    // 2. Get authentication token
    const token = await deps.tokenStore.get('default');

    const tokenValue = typeof token === 'string' ? token : undefined;

    if (!tokenValue) {
      dispatch({ type: 'bootstrap_complete', state: 'authentication-required' });
      return { bootstrap: 'authentication-required' };
    }

    // 3. Check compatibility
    try {
      const compatibility = await deps.api.compatibility();
      assertCompatible(compatibility, CLIENT_COMPATIBILITY);
    } catch {
      dispatch({ type: 'bootstrap_complete', state: 'incompatible' });
      return { bootstrap: 'incompatible' };
    }

    // 4. Get principal (user info)
    try {
      await deps.api.principal();
    } catch {
      // Principal fetch failed, but we can continue
    }

    // 5. Load last target
    if (profile?.lastTargetId) {
      try {
        const targets = await deps.api.listTargets();
        const lastTarget = targets.find((t) => t.target_id === profile?.lastTargetId);

        if (lastTarget) {
          dispatch({ type: 'set_target', target: lastTarget });
        }
      } catch {
        // Target load failed, continue without target
      }
    }

    // 6. Load last session
    if (profile?.lastSessionId) {
      try {
        const session = await deps.api.getSession(profile.lastSessionId);
        if (session) {
          dispatch({ type: 'set_session', session });
          const [messages, approvals, events] = await Promise.all([
            deps.api.listMessages(session.session_id, { limit: 500, offset: 0 }),
            deps.api.listApprovals({ sessionId: session.session_id, status: 'pending', limit: 500 }),
            deps.api.listEvents({ sessionId: session.session_id, afterSequence: 0, limit: 500 }),
          ]);
          const sequence = events.items.reduce(
            (latest, event) => Math.max(latest, event.sequence ?? 0),
            profile?.lastSequenceBySession[session.session_id] ?? 0,
          );
          dispatch({
            type: 'gap_snapshot',
            snapshot: {
              messages: messages.map((message) => ({
                kind: 'text' as const,
                messageId: message.message_id,
                blockId: message.message_id,
                content: message.content,
              })),
              operations: {},
              approvals: Object.fromEntries(
                approvals.items.map((approval) => [approval.approval_id, approval]),
              ),
              sequence,
            },
          });
        }
      } catch {
        // Session load failed, continue without session
      }
    }

    // 7. Bootstrap complete
    dispatch({ type: 'bootstrap_complete', state: 'ready' });

    return { bootstrap: 'ready' };
  } catch {
    dispatch({ type: 'bootstrap_complete', state: 'incompatible' });
    return { bootstrap: 'incompatible' };
  }
}
