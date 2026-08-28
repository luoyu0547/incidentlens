import { expect, type Page, type WebSocketRoute } from '@playwright/test';

export const ids = { service: 'svc-web', target: 'tgt-host-a', issue: 'iss-1', evidence: 'ev-1' } as const;

export const cursors = {
  1: 'lc1_AAAAAAAAAAE',
  2: 'lc1_AAAAAAAAAAI',
  3: 'lc1_AAAAAAAAAAM',
  10: 'lc1_AAAAAAAAAAo',
  11: 'lc1_AAAAAAAAAAs',
  12: 'lc1_AAAAAAAAAAw',
  13: 'lc1_AAAAAAAAAA0',
  14: 'lc1_AAAAAAAAAA4',
  15: 'lc1_AAAAAAAAAA8',
  16: 'lc1_AAAAAAAAABA',
} as const;

export const log = (n: keyof typeof cursors, message = `c${n}`, extra: Record<string, unknown> = {}) => ({
  cursor: cursors[n],
  log_id: `log-${n}`,
  message,
  occurred_at: `2026-08-26T00:00:${String(n).padStart(2, '0')}Z`,
  severity: 'info',
  ...extra,
});

export const logEvent = (event_type: string, cursor: string | null, payload: Record<string, unknown> | null = null) => ({
  schema_version: 1,
  event_type,
  occurred_at: '2026-08-26T00:00:30Z',
  cursor,
  ...(payload === null ? {} : { payload }),
});

export const overview = {
  generated_at: '2026-08-26T00:00:00Z', open_issue_count: 1, active_investigation_count: 0,
  pending_approval_count: 0, service_counts: { healthy: 0, degraded: 1 },
  targets: [{ target_id: ids.target, name: 'host-a', host: 'host-a.internal', status: 'degraded', service_count: 1,
    services: [{ service_id: ids.service, status: 'degraded', container_count: 1, open_issue_count: 1, pending_approval_count: 0 }] }],
  recent_resolutions: [],
};

export const service = {
  service_id: ids.service, status: 'degraded', generated_at: overview.generated_at,
  instances: [], investigation_ids: [], issue_ids: [ids.issue], target_ids: [ids.target], log_sources: [], pending_approval_count: 0,
};

export const issue = {
  issue_id: ids.issue, service_id: ids.service, target_id: ids.target, investigation_id: 'inv-1', status: 'open', severity: 'error',
  symptom: '响应时间升高', created_at: overview.generated_at, updated_at: overview.generated_at, root_cause: null,
  root_cause_confidence: null, started_at: overview.generated_at, completed_at: null,
  evidence: [{ evidence_ref_id: ids.evidence, summary: '连接重置', evidence_kind: 'log_record', created_at: overview.generated_at,
    service_id: ids.service, target_id: ids.target, log_cursor: 'c10' }], resolution: null, verification: null,
};

export const logPage = (items: unknown[], next_cursor: string | null = null) => ({
  has_more: Boolean(next_cursor), next_cursor, previous_cursor: null, snapshot_cursor: cursors[10], items,
});

export async function routeJson(page: Page, path: string, body: unknown) {
  await page.route(`**/api/v1${path}`, (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }));
}

export async function installCommonRoutes(page: Page) {
  await routeJson(page, '/overview', overview);
  await page.route('**/api/v1/services/*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(service) }));
  await page.route('**/api/v1/issues**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ has_more: false, next_cursor: null, items: [issue] }) }));
  await page.route('**/api/v1/issues/iss-1', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(issue) }));
  await page.route('**/api/v1/evidence/ev-1', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ content_redacted: '脱敏日志', provenance: { evidence_ref_id: ids.evidence, service_id: ids.service, target_id: ids.target, log_cursor: 'c10', created_at: overview.generated_at, evidence_kind: 'log_record', source_kind: 'file', severity: 'error' } }) }));
}

export async function installLogSocket(
  page: Page,
  onMessage: (message: Record<string, unknown>, socket: WebSocketRoute) => void,
  onOpen?: (url: string) => void,
  onClientFrame?: (message: Record<string, unknown>) => void,
) {
  await page.routeWebSocket('**/ws/v1/logs', (socket) => {
    onOpen?.(socket.url());
    socket.onMessage((message) => {
      try {
        const parsed = JSON.parse(String(message)) as Record<string, unknown>;
        onClientFrame?.(parsed);
        onMessage(parsed, socket);
      } catch { /* malformed client frames are ignored */ }
    });
  });
}

export async function expectReadOnly(page: Page) {
  const methods: string[] = [];
  page.on('request', (request) => { if (request.url().includes('/api/v1/')) methods.push(request.method()); });
  await page.waitForLoadState('networkidle');
  expect(methods.every((method) => ['GET', 'HEAD', 'OPTIONS'].includes(method))).toBe(true);
}
