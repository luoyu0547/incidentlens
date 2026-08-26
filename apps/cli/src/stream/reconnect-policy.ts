/**
 * Bounded exponential backoff for WebSocket reconnect.
 *
 * The delay starts at 250 ms and doubles on each failure, capped at 10 s.
 * The ceiling avoids unbounded wait while the floor keeps retries prompt
 * for transient (network/heartbeat) losses.
 *
 * The policy is stateless — the caller supplies the current attempt number
 * and receives the delay for that attempt. This makes it trivial to test
 * with fake timers.
 */

/**
 * Maximum delay in milliseconds (10 seconds).
 */
const MAX_DELAY_MS = 10_000;

/**
 * Initial delay in milliseconds (250 ms).
 */
const INITIAL_DELAY_MS = 250;

/**
 * Compute the backoff delay for a given reconnect attempt.
 *
 * @param attempt - 0-based attempt counter.
 * @returns Delay in milliseconds for this attempt.
 */
export function backoffDelay(attempt: number): number {
  const delay = INITIAL_DELAY_MS * Math.pow(2, attempt);
  return Math.min(delay, MAX_DELAY_MS);
}