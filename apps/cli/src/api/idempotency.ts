import { randomUUID } from 'node:crypto';

/**
 * Create a new idempotency key for mutations.
 * Uses crypto.randomUUID() for uniqueness.
 */
export function createIdempotencyKey(): string {
  return randomUUID();
}

/**
 * Idempotency key header name.
 */
export const IDEMPOTENCY_KEY_HEADER = 'Idempotency-Key';
