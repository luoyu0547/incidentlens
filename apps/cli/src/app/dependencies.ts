/**
 * Application dependencies for dependency injection.
 *
 * All external services are injected through this interface,
 * making the app testable and decoupled from concrete implementations.
 */

import type { ControlPlaneApi } from '../api/control-plane-api.js';
import type { ConfigStore } from '../config/types.js';
import type { TokenStore } from '../auth/token-store.js';

/**
 * Event stream interface for WebSocket connections.
 */
export interface EventStream {
  connect(
    cursor: { sessionId: string; sequence: number },
    handlers: {
      onEvent: (event: any) => Promise<void> | void;
      onGap: (gap: any) => Promise<void>;
      onStatus: (status: { connected: boolean; error?: string }) => void;
    },
    signal: AbortSignal
  ): Promise<void>;
}

/**
 * Application dependencies injected into the App component.
 */
export interface AppDependencies {
  readonly api: ControlPlaneApi;
  readonly configStore: ConfigStore;
  readonly tokenStore: TokenStore;
  readonly eventStream: EventStream;
  readonly now: () => Date;
  readonly exit: () => void;
}
