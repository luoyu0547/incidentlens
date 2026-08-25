/**
 * Bootstrap sequence for IncidentLens CLI.
 *
 * Handles the initialization order:
 * profile → token → compatibility → principal → target → Session snapshot → event stream
 */

import type { AppDependencies } from './dependencies.js';
import type { CliState, CliAction } from '../state/cli-state.js';

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

    if (!token) {
      dispatch({ type: 'bootstrap_complete', state: 'authentication-required' });
      return { bootstrap: 'authentication-required' };
    }

    // 3. Check compatibility
    try {
      const compatibility = await deps.api.compatibility();

      if (!compatibility.compatible) {
        dispatch({ type: 'bootstrap_complete', state: 'incompatible' });
        return { bootstrap: 'incompatible' };
      }
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
        const lastTarget = targets.find((t) => t.id === profile?.lastTargetId);

        if (lastTarget) {
          dispatch({ type: 'set_target', target: lastTarget });
        }
      } catch {
        // Target load failed, continue without target
      }
    }

    // 6. Bootstrap complete
    dispatch({ type: 'bootstrap_complete', state: 'ready' });

    return { bootstrap: 'ready' };
  } catch {
    dispatch({ type: 'bootstrap_complete', state: 'incompatible' });
    return { bootstrap: 'incompatible' };
  }
}
