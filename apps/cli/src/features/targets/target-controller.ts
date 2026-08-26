/**
 * Target controller for IncidentLens CLI.
 *
 * Wraps the control plane target surface and the profile config store.
 * The controller is the single dependency the command layer and the
 * target wizard use to list, select, create, update, test, and remove
 * remote targets.
 *
 * Security rules:
 * - Mutations send metadata plus an opaque `authentication_ref`; the
 *   CLI never accepts or forwards private-key material.
 * - `/target test` only relays the server's safe, redacted operation
 *   result (verified host-key source/fingerprint or a safe failure).
 * - The selected target is mirrored into `state.target` and persisted
 *   as `lastTargetId` in the active profile so it survives restarts.
 */

import { createIdempotencyKey } from '../../api/idempotency.js';
import type { ControlPlaneApi } from '../../api/control-plane-api.js';
import type { ConfigStore } from '../../config/types.js';
import type { CliAction } from '../../state/cli-state.js';
import type {
  OperationAccepted,
  OperationStatus,
  OperationView,
  TargetCreate,
  TargetPatch,
  TargetView,
} from '@incidentlens/protocol';

/**
 * Target controller interface.
 *
 * The minimal contract consumed by command handlers.
 */
export interface TargetController {
  list(signal?: AbortSignal): Promise<readonly TargetView[]>;
  select(target: TargetView): Promise<void>;
  create(input: TargetCreate): Promise<TargetView>;
  update(id: string, input: TargetPatch): Promise<TargetView>;
  test(id: string): Promise<OperationAccepted>;
  remove(id: string): Promise<void>;
}

/**
 * Options for constructing a target controller.
 */
export interface TargetControllerOptions {
  readonly api: ControlPlaneApi;
  readonly configStore: ConfigStore;
  readonly profileName: string;
  readonly dispatch?: (action: CliAction) => void;
}

/**
 * Concrete target controller.
 *
 * Supports the `TargetController` contract plus two implementation
 * conveniences the wizard and the `test` flow rely on:
 * - mutation methods accept an optional trailing idempotency key so an
 *   interrupted write can be retried with the same key (deduplicated),
 * - `getOperation` lets the `/target test` flow follow a returned
 *   operation to a terminal state.
 */
export class TargetController implements TargetController {
  private readonly api: ControlPlaneApi;
  private readonly configStore: ConfigStore;
  private readonly profileName: string;
  private readonly dispatch?: (action: CliAction) => void;

  constructor(options: TargetControllerOptions) {
    this.api = options.api;
    this.configStore = options.configStore;
    this.profileName = options.profileName;
    this.dispatch = options.dispatch;
  }

  async list(signal?: AbortSignal): Promise<readonly TargetView[]> {
    return this.api.listTargets(signal);
  }

  async select(target: TargetView): Promise<void> {
    this.dispatch?.({ type: 'set_target', target });

    // Persist the selection so it can be restored on the next launch.
    // Only write when a profile already exists; we never fabricate one.
    const existing = await this.configStore.load(this.profileName);
    if (existing) {
      await this.configStore.save({ ...existing, lastTargetId: target.target_id });
    }
  }

  async create(input: TargetCreate, idempotencyKey?: string): Promise<TargetView> {
    return this.api.createTarget(input, {
      idempotencyKey: idempotencyKey ?? createIdempotencyKey(),
    });
  }

  async update(id: string, input: TargetPatch, idempotencyKey?: string): Promise<TargetView> {
    return this.api.updateTarget(id, input, {
      idempotencyKey: idempotencyKey ?? createIdempotencyKey(),
    });
  }

  async test(id: string, idempotencyKey?: string): Promise<OperationAccepted> {
    return this.api.testTarget(id, {
      idempotencyKey: idempotencyKey ?? createIdempotencyKey(),
    });
  }

  async remove(id: string, idempotencyKey?: string): Promise<void> {
    await this.api.removeTarget(id, {
      idempotencyKey: idempotencyKey ?? createIdempotencyKey(),
    });
  }

  getOperation(id: string, signal?: AbortSignal): Promise<OperationView> {
    return this.api.getOperation(id, signal);
  }
}

/**
 * Safe progress result of a target-test operation.
 *
 * Only carries server-redacted fields (`progress_summary`,
 * `error_message`); raw SSH output and credentials never reach this.
 */
export interface TargetTestProgress {
  readonly status: OperationStatus;
  readonly summary: string | null;
  readonly error: string | null;
  readonly operationId: string;
}

/**
 * Follow a target-test operation to a terminal state and report the
 * server's safe result (verified host-key source/fingerprint or a safe
 * failure). Never reconstructs or displays credentials.
 *
 * @param getOperation - Resolves an operation by id.
 * @param operationId - The operation accepted by `/targets/{id}/test`.
 * @param onResult - Called once with the terminal progress.
 * @param options - Poll tuning (mainly for tests).
 */
export async function trackTargetTest(
  getOperation: (operationId: string, signal?: AbortSignal) => Promise<OperationView>,
  operationId: string,
  onResult: (progress: TargetTestProgress) => void,
  options: { pollIntervalMs?: number; maxPolls?: number } = {}
): Promise<TargetTestProgress> {
  const pollIntervalMs = options.pollIntervalMs ?? 400;
  const maxPolls = options.maxPolls ?? 150;

  let latest: OperationView | undefined;
  let reachedTerminal = false;
  for (let attempt = 0; attempt < maxPolls; attempt += 1) {
    const operation = await getOperation(operationId);
    latest = operation;

    if (
      operation.status === 'succeeded' ||
      operation.status === 'failed' ||
      operation.status === 'cancelled'
    ) {
      reachedTerminal = true;
      break;
    }

    await delay(pollIntervalMs);
  }

  const progress: TargetTestProgress = {
    status: reachedTerminal ? (latest?.status ?? 'uncertain') : 'uncertain',
    summary: latest?.progress_summary ?? null,
    error: latest?.error_message ?? null,
    operationId,
  };

  onResult(progress);
  return progress;
}

/**
 * Short sleep used by the operation poll loop.
 */
function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
