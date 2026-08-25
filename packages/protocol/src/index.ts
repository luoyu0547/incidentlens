// Re-export generated types, SDK functions, and client
export * from './generated/index.js';

// Re-export stream parsing utilities
export {
  CliStreamEnvelopeSchema,
  LogStreamEnvelopeSchema,
  WorkspaceStreamEnvelopeSchema,
  parseStreamFrame,
  assertCompatible,
  ProtocolError,
} from './stream.js';

export type {
  JsonValue,
  CliStreamEnvelope,
  CliStreamEnvelopeBase,
  KnownCliStreamEnvelope,
  LogStreamEnvelope,
  WorkspaceStreamEnvelope,
  ParsedStreamEvent,
  ApiVersionView,
  ClientCompatibility,
} from './stream.js';
