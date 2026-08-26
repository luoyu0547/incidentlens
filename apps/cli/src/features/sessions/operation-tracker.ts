/**
 * Operation tracker for IncidentLens CLI.
 *
 * Follows durable Agent operations (message enqueue, resume, cancel) to a
 * terminal state and reports only server-redacted progress. This mirrors
 * the target-test tracker but is generic across operation kinds.
 *
 * Safety rules:
 * - Only the safe `OperationView` projection (progress_summary,
 *   error_message, status) is surfaced. Raw tool args/output, provider
 *   payloads, hidden reasoning, and canonical intents never appear here.
 */

import type { OperationStatus, OperationView } from '@incidentlens/protocol';

/**
 * Safe progress result of an operation follow.
 */
export interface OperationTrackingProgress {
  readonly status: OperationStatus;
  readonly summary: string | null;
  readonly error: string | null;
  readonly operationId: string;
  readonly kind: string;
}

/**
 * Given a way to resolve an operation by id, poll until it reaches a
 * terminal state and report the safe progress.
 *
 * @param getOperation - Resolves an operation by id.
 * @param operationId - The operation id returned by an accepted action.
 * @param onResult - Called once with the terminal progress (or error).
 * @param options - Poll tuning and optional AbortSignal.
 * @param options.onError - Optional callback for safe error display.
 */
export async function trackOperation<
  GetOperation extends (operationId: string, signal?: AbortSignal) => Promise<OperationView>,
>(
  getOperation: GetOperation,
  operationId: string,
  onResult: (progress: OperationTrackingProgress) => void,
  options: {
    pollIntervalMs?: number;
    maxPolls?: number;
    signal?: AbortSignal;
    onError?: (error: string) => void;
  } = {},
): Promise<OperationTrackingProgress> {
  const pollIntervalMs = options.pollIntervalMs ?? 400;
  const maxPolls = options.maxPolls ?? 150;
  const errorCallback = options.onError;

  let latest: OperationView | undefined;
  let reachedTerminal = false;
  let apiError: string | undefined;

  for (let attempt = 0; attempt < maxPolls; attempt += 1) {
    try {
      const operation = await getOperation(operationId, options.signal);
      latest = operation;

      if (
        operation.status === 'succeeded' ||
        operation.status === 'failed' ||
        operation.status === 'cancelled'
      ) {
        reachedTerminal = true;
        break;
      }
    } catch (error) {
      // Network / server failure — report as safe failure and stop.
      const message =
        error instanceof Error ? error.message : 'Operation polling failed';
      apiError = message;
      break;
    }

    await delay(pollIntervalMs);
  }

  const progress: OperationTrackingProgress = {
    status: apiError
      ? 'failed'
      : reachedTerminal
        ? (latest?.status ?? 'uncertain')
        : 'uncertain',
    summary: latest?.progress_summary ?? null,
    error: apiError ?? (latest?.error_message ?? null),
    operationId,
    kind: latest?.kind ?? 'agent_message',
  };

  if (apiError) {
    errorCallback?.(apiError);
  }

  onResult(progress);
  return progress;
}

/**
 * Short sleep used by the operation poll loop.
 */
function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}