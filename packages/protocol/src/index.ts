export {
  connectWorkspaceEvents,
  WorkspaceGapEventSchema,
  WorkspaceResourceEventSchema,
} from './workspace-events.js';

export type {
  WorkspaceEventConnection,
  WorkspaceEventOptions,
  WorkspaceEventStatus,
  WorkspaceGapEvent,
  WorkspaceResourceEvent,
} from './workspace-events.js';

// must not be reachable from the web workspace through the root package entry)
export type * from './generated/types.gen.js';

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

export {
  LogSubscribeCommandSchema,
  LogUpdateCommandSchema,
  LogPauseCommandSchema,
  LogResumeCommandSchema,
  LogAckCommandSchema,
  LogStreamCommandSchema,
  LogStreamEventSchema,
  parseLogStreamCommand,
  parseLogStreamEvent,
  serializeLogSubscribe,
  serializeLogUpdate,
  serializeLogPause,
  serializeLogResume,
  serializeLogAck,
  createLogSubscribe,
  createLogUpdate,
  createLogPause,
  createLogResume,
  createLogAck,
} from './log-stream.js';

export type {
  LogSubscribeCommand,
  LogUpdateCommand,
  LogPauseCommand,
  LogResumeCommand,
  LogAckCommand,
  LogStreamCommand,
  LogStreamEvent,
  KnownLogStreamEvent,
  LogRecordEvent,
} from './log-stream.js';

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
