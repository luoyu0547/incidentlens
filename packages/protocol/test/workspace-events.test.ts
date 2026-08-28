import { describe, expect, it } from 'vitest';
import { WorkspaceGapEventSchema, WorkspaceResourceEventSchema } from '../src/workspace-events';

describe('workspace event schemas', () => {
  it('parses resource changes and rejects unknown fields', () => {
    const event = WorkspaceResourceEventSchema.parse({ schema_version: 1, event_id: 'e1', event_type: 'resource.changed', occurred_at: '2026-01-01T00:00:00.000Z', resource_kind: 'service', resource_id: 'svc' });
    expect(event.resource_id).toBe('svc');
    expect(WorkspaceResourceEventSchema.safeParse({ ...event, extra: true }).success).toBe(false);
  });
  it('parses gaps', () => {
    expect(WorkspaceGapEventSchema.parse({ schema_version: 1, event_id: 'e1', event_type: 'stream.gap', occurred_at: '2026-01-01T00:00:00.000Z', reason: 'missing', action: 'reload_snapshot' }).action).toBe('reload_snapshot');
  });
});
