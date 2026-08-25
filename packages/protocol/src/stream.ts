import { z } from 'zod';

// ---------------------------------------------------------------------------
// CLI Stream schema  (incidentlens://protocol/cli-stream-v1)
// ---------------------------------------------------------------------------

/** JSON-serialisable value – mirrors the schema's JsonValue def. */
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

// ---- Zod schemas ---------------------------------------------------------

/** Runtime event types that the CLI stream currently recognises. */
const runtimeEventTypes = [
  'project.created',
  'project.updated',
  'project.deleted',
  'approval.requested',
  'approval.approved',
  'approval.rejected',
  'approval.consumed',
  'changeset.created',
  'changeset.status_changed',
  'changeset.rolled_back',
  'docker_action.requested',
  'docker_action.started',
  'docker_action.completed',
  'docker_action.failed',
  'remote_session.connected',
  'remote_session.disconnected',
  'remote_session.failed',
  'remote_operation.started',
  'remote_operation.completed',
  'log.subscription_started',
  'log.subscription_paused',
  'log.subscription_resumed',
  'log.subscription_deleted',
  'log.batch_written',
  'log.source_rotated',
  'log.backpressure',
  'log.subscription_error',
  'investigation.created',
  'investigation.started',
  'investigation.status_changed',
  'investigation.completed',
  'investigation.cancelled',
  'investigation.failed',
  'operation.queued',
  'operation.running',
  'operation.cancel_requested',
  'operation.succeeded',
  'operation.failed',
  'operation.cancelled',
  'operation.uncertain',
  'agent_run.started',
  'agent_run.status_changed',
  'agent_run.completed',
  'agent_run.failed',
  'agent_run.cancelled',
  'agent_hook',
  'agent.text.delta',
  'agent.message.completed',
  'model_round.started',
  'model_round.completed',
  'tool.proposed',
  'policy.decided',
  'todo.changed',
  'hypothesis.changed',
  'conclusion.created',
  'context.compacted',
  'safety_state.changed',
  'tool_call.started',
  'tool_call.status_changed',
  'tool_call.completed',
  'child_run.started',
  'child_run.completed',
  'evidence.appended',
  'registry_proposal.created',
  'registry_proposal.decided',
  'recovery.started',
  'recovery.completed',
] as const;

const streamControlTypes = [
  'stream.hello',
  'stream.heartbeat',
  'stream.gap',
  'stream.slow_consumer',
] as const;

const jsonValueSchema: z.ZodType<JsonValue> = z.lazy(() =>
  z.union([
    z.string(),
    z.number(),
    z.boolean(),
    z.null(),
    z.array(jsonValueSchema),
    z.record(z.string(), jsonValueSchema),
  ]),
);

/** Zod schema for a CLI stream event envelope (cli-stream-v1). */
export const CliStreamEnvelopeSchema = z
  .object({
    schema_version: z.literal(1),
    event_type: z.union([
      z.enum(streamControlTypes),
      z.enum(runtimeEventTypes as unknown as [string, ...string[]]),
    ]),
    occurred_at: z.string().datetime(),
    sequence: z.union([z.number().int().nonnegative(), z.null()]).optional(),
    event_id: z.union([z.string(), z.null()]).optional(),
    investigation_id: z.union([z.string(), z.null()]).optional(),
    session_id: z.union([z.string(), z.null()]).optional(),
    target_id: z.union([z.string(), z.null()]).optional(),
    payload: z.union([z.record(z.string(), jsonValueSchema), z.null()]).optional(),
  })
  .strict();

export type CliStreamEnvelope = z.infer<typeof CliStreamEnvelopeSchema>;

// ---- Log stream schema (incidentlens://protocol/log-stream-v1) -----------

export const LogStreamEnvelopeSchema = z
  .object({
    schema_version: z.literal(1),
    event_type: z.string().min(1),
    occurred_at: z.string().datetime(),
    cursor: z.union([z.string(), z.null()]).optional(),
    payload: z.union([z.record(z.string(), jsonValueSchema), z.null()]).optional(),
  })
  .strict();

export type LogStreamEnvelope = z.infer<typeof LogStreamEnvelopeSchema>;

// ---- Workspace stream schema (incidentlens://protocol/workspace-stream-v1)

export const WorkspaceStreamEnvelopeSchema = z
  .object({
    schema_version: z.literal(1),
    event_type: z.string().min(1),
    sequence: z.number().int().nonnegative(),
    event_id: z.string().optional(),
    payload: z.record(z.string(), z.unknown()).optional(),
    resource_type: z.string().optional(),
  })
  .strict();

export type WorkspaceStreamEnvelope = z.infer<typeof WorkspaceStreamEnvelopeSchema>;

// ---- Parsed stream event types -------------------------------------------

/** Base fields present in every envelope after parsing. */
export interface CliStreamEnvelopeBase {
  schema_version: 1;
  event_type: string;
  occurred_at: string;
  sequence?: number | null;
  event_id?: string | null;
  investigation_id?: string | null;
  session_id?: string | null;
  target_id?: string | null;
  payload?: Record<string, JsonValue> | null;
}

export interface KnownCliStreamEnvelope extends CliStreamEnvelopeBase {
  event_type:
    | (typeof streamControlTypes)[number]
    | (typeof runtimeEventTypes)[number];
}

export type ParsedStreamEvent =
  | { kind: 'known'; envelope: KnownCliStreamEnvelope }
  | { kind: 'unknown'; envelope: CliStreamEnvelopeBase };

// ---- API version compatibility types -------------------------------------

import type { ApiVersionView as GeneratedApiVersionView } from './generated/types.gen.js';

export type { GeneratedApiVersionView as ApiVersionView };

export interface ClientCompatibility {
  min_protocol_version: string;
  max_protocol_version: string;
}

// ---- parseStreamFrame ----------------------------------------------------

/** All known event-type strings for fast membership check. */
const knownEventTypes = new Set<string>([
  ...streamControlTypes,
  ...runtimeEventTypes,
]);

/**
 * Parse a raw WebSocket text frame into a typed stream event.
 *
 * Errors:
 *   - Malformed JSON → throws
 *   - Missing `schema_version` or `event_type` → throws
 *   - Unsupported `schema_version` → throws
 *   - Known event_type → `kind: 'known'`
 *   - Unknown event_type → `kind: 'unknown'` (safe to advance cursor)
 */
export function parseStreamFrame(raw: string): ParsedStreamEvent {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new ProtocolError('MALFORMED_JSON', 'Frame is not valid JSON');
  }

  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new ProtocolError('NOT_OBJECT', 'Frame must be a JSON object');
  }

  const obj = parsed as Record<string, unknown>;

  // --- schema_version (required, must be 1) ---
  if (obj.schema_version !== 1) {
    throw new ProtocolError(
      'UNSUPPORTED_SCHEMA_VERSION',
      `Expected schema_version 1, got ${String(obj.schema_version)}`,
    );
  }

  // --- event_type (required) ---
  if (typeof obj.event_type !== 'string' || obj.event_type.length === 0) {
    throw new ProtocolError(
      'MISSING_EVENT_TYPE',
      'Frame must include a non-empty event_type string',
    );
  }

  // --- occurred_at (required) ---
  if (typeof obj.occurred_at !== 'string') {
    throw new ProtocolError(
      'MISSING_OCCURRED_AT',
      'Frame must include an occurred_at string',
    );
  }

  const base: CliStreamEnvelopeBase = {
    schema_version: 1,
    event_type: obj.event_type,
    occurred_at: obj.occurred_at,
    sequence: typeof obj.sequence === 'number' ? obj.sequence : null,
    event_id: typeof obj.event_id === 'string' ? obj.event_id : null,
    investigation_id: typeof obj.investigation_id === 'string' ? obj.investigation_id : null,
    session_id: typeof obj.session_id === 'string' ? obj.session_id : null,
    target_id: typeof obj.target_id === 'string' ? obj.target_id : null,
    payload:
      obj.payload != null && typeof obj.payload === 'object' && !Array.isArray(obj.payload)
        ? (obj.payload as Record<string, JsonValue>)
        : null,
  };

  if (knownEventTypes.has(obj.event_type)) {
    return { kind: 'known', envelope: base as KnownCliStreamEnvelope };
  }

  return { kind: 'unknown', envelope: base };
}

// ---- assertCompatible ----------------------------------------------------

/** Thrown when client/server protocol versions are incompatible. */
export class ProtocolError extends Error {
  constructor(
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'ProtocolError';
  }
}

/**
 * Assert that the server's advertised version is within the client's
 * supported range.  Throws `ProtocolError` on incompatibility.
 */
export function assertCompatible(
  server: GeneratedApiVersionView,
  client: ClientCompatibility,
): void {
  const minCliVersion = server.minimum_cli_protocol_version;
  if (!minCliVersion) {
    throw new ProtocolError(
      'MISSING_PROTOCOL_VERSION',
      'Server version response does not include minimum_cli_protocol_version',
    );
  }

  if (minCliVersion < client.min_protocol_version) {
    throw new ProtocolError(
      'VERSION_TOO_OLD',
      `Server minimum_cli_protocol_version ${minCliVersion} is below client minimum ${client.min_protocol_version}`,
    );
  }

  if (minCliVersion > client.max_protocol_version) {
    throw new ProtocolError(
      'VERSION_TOO_NEW',
      `Server minimum_cli_protocol_version ${minCliVersion} exceeds client maximum ${client.max_protocol_version}`,
    );
  }
}
