// Re-export generated types, SDK functions, and client
export * from './generated/index.js';

// Re-export the read-only web facade (all apps/web HTTP must go through it)
export {
  DEFAULT_BASE_URL,
  createWebReadonlyClient,
  ReadonlyApiError,
} from './web-readonly-client.js';

export type {
  WebReadonlyClient,
  WebReadonlyClientOptions,
  ReadonlyApiErrorOptions,
  TargetPage,
  TargetServicePage,
  ServiceLogQuery,
  IssueListQuery,
  InvestigationListQuery,
} from './web-readonly-client.js';

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
