import { z } from 'zod';
import type { LogRecordView } from './generated/types.gen.js';

const cursor = z.string().min(1);
const optionalCursor = cursor.nullable().optional();

export const LogSubscribeCommandSchema = z.object({
  action: z.literal('subscribe'), service_id: z.string().min(1), target_id: z.string().min(1).optional(),
  severity: z.string().min(1).optional(), source_ref: z.string().min(1).optional(), cursor: optionalCursor,
}).strict();
export const LogUpdateCommandSchema = z.object({
  action: z.literal('update'), service_id: z.string().min(1).optional(), target_id: z.string().min(1).nullable().optional(),
  severity: z.string().min(1).nullable().optional(), source_ref: z.string().min(1).nullable().optional(), cursor: optionalCursor,
}).strict();
export const LogPauseCommandSchema = z.object({ action: z.literal('pause') }).strict();
export const LogResumeCommandSchema = z.object({ action: z.literal('resume'), cursor: optionalCursor }).strict();
export const LogAckCommandSchema = z.object({ action: z.literal('ack'), cursor }).strict();
export const LogStreamCommandSchema = z.discriminatedUnion('action', [LogSubscribeCommandSchema, LogUpdateCommandSchema, LogPauseCommandSchema, LogResumeCommandSchema, LogAckCommandSchema]);
export type LogSubscribeCommand = z.infer<typeof LogSubscribeCommandSchema>;
export type LogUpdateCommand = z.infer<typeof LogUpdateCommandSchema>;
export type LogPauseCommand = z.infer<typeof LogPauseCommandSchema>;
export type LogResumeCommand = z.infer<typeof LogResumeCommandSchema>;
export type LogAckCommand = z.infer<typeof LogAckCommandSchema>;
export type LogStreamCommand = z.infer<typeof LogStreamCommandSchema>;

export const LogStreamEventSchema = z.object({
  schema_version: z.literal(1), event_type: z.string().min(1), occurred_at: z.string().datetime(),
  cursor: z.string().nullable().optional(), payload: z.record(z.string(), z.unknown()).nullable().optional(),
}).strict();
export type LogStreamEvent = z.infer<typeof LogStreamEventSchema>;
export type KnownLogStreamEvent = LogStreamEvent & { event_type: 'log.subscribed'|'log.record'|'stream.heartbeat'|'stream.gap'|'stream.slow_consumer' };
export type LogRecordEvent = KnownLogStreamEvent & { event_type: 'log.record'; payload: LogRecordView };
const known = new Set(['log.subscribed','log.record','stream.heartbeat','stream.gap','stream.slow_consumer']);

export function parseLogStreamCommand(value: unknown): LogStreamCommand { return LogStreamCommandSchema.parse(value); }
export function parseLogStreamEvent(value: unknown): KnownLogStreamEvent | { kind: 'unknown'; event: LogStreamEvent } {
  const event = LogStreamEventSchema.parse(value);
  return known.has(event.event_type) ? event as KnownLogStreamEvent : { kind: 'unknown', event };
}
export function serializeLogSubscribe(command: Omit<LogSubscribeCommand, 'action'>): LogSubscribeCommand { return LogSubscribeCommandSchema.parse({ action:'subscribe', ...command }); }
export function serializeLogUpdate(command: Omit<LogUpdateCommand, 'action'>): LogUpdateCommand { return LogUpdateCommandSchema.parse({ action:'update', ...command }); }
export function serializeLogPause(): LogPauseCommand { return { action:'pause' }; }
export function serializeLogResume(cursorValue?: string | null): LogResumeCommand { return LogResumeCommandSchema.parse({ action:'resume', ...(cursorValue == null ? {} : {cursor:cursorValue}) }); }
export function serializeLogAck(cursorValue: string): LogAckCommand { return LogAckCommandSchema.parse({ action:'ack', cursor:cursorValue }); }
export const createLogSubscribe = serializeLogSubscribe;
export const createLogUpdate = serializeLogUpdate;
export const createLogPause = serializeLogPause;
export const createLogResume = serializeLogResume;
export const createLogAck = serializeLogAck;
