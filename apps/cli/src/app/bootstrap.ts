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

    // 4. Get principal (user info). Authentication failures are fatal: never
    // enter a ready state with an unverified principal.
    try {
      await deps.api.principal();
    } catch {
      dispatch({ type: 'bootstrap_complete', state: 'authentication-required' });
      return { bootstrap: 'authentication-required' };
    }

    // 5. Load last target
    try {
      const targets = await deps.api.listTargets();
      const remembered = profile?.lastTargetId
        ? targets.find((t) => t.target_id === profile.lastTargetId)
        : undefined;
      // A single configured target is an unambiguous workspace default. This
      // keeps a fresh demo session immediately actionable while preserving the
      // picker behavior when multiple targets exist.
      const target = remembered ?? (targets.length === 1 ? targets[0] : undefined);
      if (target) {
        dispatch({ type: 'set_target', target });
      }
    } catch {
      // Target load failed, continue without target
    }

    // A fresh CLI launch is a fresh conversation. Historical session state is
    // loaded only through the explicit /resume command, never implicitly from
    // the persisted lastSessionId. This prevents old tools and approvals from
    // appearing before the operator has submitted a new request.

    // 7. Bootstrap complete
    dispatch({ type: 'bootstrap_complete', state: 'ready' });

    return { bootstrap: 'ready' };
  } catch {
    dispatch({ type: 'bootstrap_complete', state: 'incompatible' });
    return { bootstrap: 'incompatible' };
  }
}
