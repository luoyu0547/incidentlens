import { z } from 'zod';

/** A resource snapshot invalidation emitted by the workspace SSE stream. */
export const WorkspaceResourceEventSchema = z.object({
  schema_version: z.literal(1),
  event_id: z.string().min(1),
  event_type: z.literal('resource.changed'),
  occurred_at: z.string().datetime(),
  resource_kind: z.enum(['overview', 'target', 'service', 'issue', 'investigation', 'evidence']),
  resource_id: z.string().nullable().optional().default(null),
  target_id: z.string().nullable().optional().default(null),
  service_id: z.string().nullable().optional().default(null),
}).strict();

export const WorkspaceGapEventSchema = z.object({
  schema_version: z.literal(1),
  event_id: z.string().min(1),
  event_type: z.literal('stream.gap'),
  occurred_at: z.string().datetime(),
  reason: z.string(),
  action: z.literal('reload_snapshot'),
}).strict();

export type WorkspaceResourceEvent = z.infer<typeof WorkspaceResourceEventSchema>;
export type WorkspaceGapEvent = z.infer<typeof WorkspaceGapEventSchema>;

export interface WorkspaceEventConnection { close(): void; }
export type WorkspaceEventStatus = 'connecting' | 'live' | 'reconnecting' | 'authentication-error' | 'closed';

export interface WorkspaceEventOptions {
  url?: string;
  afterEventId?: string;
  onResourceChanged(event: WorkspaceResourceEvent): void;
  onGap(event: WorkspaceGapEvent): void;
  onStatus(status: WorkspaceEventStatus): void;
}

const STORAGE_KEY = 'incidentlens.workspace.last-event-id';
const DEFAULT_URL = '/events/v1/workspace';
const MAX_RETRIES = 5;
const RETRY_BASE_MS = 250;

function readLastEventId(): string | undefined {
  try { return sessionStorage.getItem(STORAGE_KEY) ?? undefined; } catch { return undefined; }
}
function saveLastEventId(id: string): void {
  try { sessionStorage.setItem(STORAGE_KEY, id); } catch { /* storage is optional */ }
}

/** Connect to the authenticated workspace invalidation stream. */
export function connectWorkspaceEvents(options: WorkspaceEventOptions): WorkspaceEventConnection {
  let source: EventSource | undefined;
  let closed = false;
  let retries = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let cursor = options.afterEventId ?? readLastEventId();

  const status = (value: WorkspaceEventStatus) => { if (!closed) options.onStatus(value); };
  const connect = () => {
    if (closed) return;
    status(retries === 0 ? 'connecting' : 'reconnecting');
    const browserOrigin = (globalThis as { location?: { origin: string } }).location?.origin;
    const url = new URL(options.url ?? DEFAULT_URL, browserOrigin ?? 'http://localhost');
    if (cursor) url.searchParams.set('after_event_id', cursor);
    source = new EventSource(url.toString());
    source.onopen = () => { retries = 0; status('live'); };
    source.onmessage = (message) => handle(message.data, message.lastEventId);
    source.addEventListener('resource.changed', (event) => handle((event as MessageEvent).data, (event as MessageEvent).lastEventId));
    source.addEventListener('stream.gap', (event) => handle((event as MessageEvent).data, (event as MessageEvent).lastEventId));
    source.onerror = () => {
      const responseStatus = (source as (EventSource & { status?: number }) | undefined)?.status;
      source?.close(); source = undefined;
      if (closed) return;
      if (responseStatus === 401 || responseStatus === 403) {
        closed = true; options.onStatus('authentication-error'); return;
      }
      if (retries >= MAX_RETRIES) { closed = true; options.onStatus('closed'); return; }
      const delay = RETRY_BASE_MS * 2 ** retries++;
      timer = setTimeout(connect, delay);
    };
  };
  const handle = (raw: string, eventId?: string) => {
    let parsed: unknown;
    try { parsed = JSON.parse(raw); } catch { return; }
    const candidate = parsed as { event_type?: string };
    const result = candidate.event_type === 'resource.changed'
      ? WorkspaceResourceEventSchema.safeParse(parsed)
      : candidate.event_type === 'stream.gap' ? WorkspaceGapEventSchema.safeParse(parsed) : null;
    if (!result || !result.success) return;
    const id = eventId || result.data.event_id;
    if (id) { cursor = id; saveLastEventId(id); }
    if (result.data.event_type === 'resource.changed') options.onResourceChanged(result.data);
    else options.onGap(result.data);
  };
  connect();
  return { close() { if (closed) return; closed = true; if (timer) clearTimeout(timer); source?.close(); source = undefined; options.onStatus('closed'); } };
}
