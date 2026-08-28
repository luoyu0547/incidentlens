/**
 * Test fixtures for the observability web workspace.
 *
 * These are minimal, valid-shaped snapshots used by MSW handlers. Every fixture
 * matches the generated protocol types so the web UI can consume them directly.
 */
import type {
  OverviewView,
  ServiceDetailView,
  IssueView,
  IssuePage,
  LogPage,
  InvestigationSummaryView,
  InvestigationSummaryPage,
  EvidenceDetailView,
  TargetView,
  TargetServiceView,
} from '@incidentlens/protocol';

/** Stable service identifiers used across fixtures. */
export const TEST_SERVICE_ID = 'svc-web';
export const TEST_SERVICE_ID_2 = 'svc-db';
export const TEST_ISSUE_ID = 'iss-1';
export const TEST_INVESTIGATION_ID = 'inv-1';
export const TEST_EVIDENCE_ID = 'ev-1';
export const TEST_TARGET_ID = 'tgt-host-a';

export const overviewFixture: OverviewView = {
  generated_at: '2026-08-26T00:00:00Z',
  open_issue_count: 1,
  active_investigation_count: 1,
  pending_approval_count: 0,
  service_counts: { healthy: 1, degraded: 1 },
  targets: [
    {
      target_id: TEST_TARGET_ID,
      name: 'host-a',
      host: 'host-a.internal',
      status: 'healthy',
      service_count: 1,
      services: [
        {
          service_id: TEST_SERVICE_ID,
          status: 'degraded',
          container_count: 2,
          open_issue_count: 1,
          pending_approval_count: 0,
        },
      ],
    },
  ],
  recent_resolutions: [],
};

export const serviceFixture: ServiceDetailView = {
  service_id: TEST_SERVICE_ID,
  status: 'degraded',
  generated_at: '2026-08-26T00:00:00Z',
  instances: [],
  investigation_ids: [TEST_INVESTIGATION_ID],
  issue_ids: [TEST_ISSUE_ID],
  target_ids: [TEST_TARGET_ID],
  log_sources: [],
  pending_approval_count: 0,
};

export const issueFixture: IssueView = {
  issue_id: TEST_ISSUE_ID,
  service_id: TEST_SERVICE_ID,
  target_id: TEST_TARGET_ID,
  investigation_id: TEST_INVESTIGATION_ID,
  status: 'open',
  severity: 'error',
  symptom: 'web 服务响应时间升高',
  created_at: '2026-08-26T00:00:00Z',
  updated_at: '2026-08-26T00:00:00Z',
  root_cause: null,
  root_cause_confidence: null,
  started_at: '2026-08-26T00:00:00Z',
  completed_at: null,
};

export const issuePageFixture: IssuePage = {
  has_more: false,
  next_cursor: null,
  items: [issueFixture],
};

export const logPageFixture: LogPage = {
  has_more: false,
  next_cursor: null,
  previous_cursor: null,
  snapshot_cursor: 'snap-1',
  items: [
    {
      cursor: 'cursor-1',
      log_id: 'log-1',
      message: 'connection reset by peer',
      occurred_at: '2026-08-26T00:00:00Z',
      severity: 'error',
    },
  ],
};

export const investigationFixture: InvestigationSummaryView = {
  investigation_id: TEST_INVESTIGATION_ID,
  service_id: TEST_SERVICE_ID,
  target_id: TEST_TARGET_ID,
  issue_id: TEST_ISSUE_ID,
  status: 'running',
  symptom: 'web 服务响应时间升高',
  created_at: '2026-08-26T00:00:00Z',
  started_at: '2026-08-26T00:00:00Z',
  updated_at: '2026-08-26T00:00:00Z',
  completed_at: null,
};

export const investigationPageFixture: InvestigationSummaryPage = {
  has_more: false,
  next_cursor: null,
  items: [investigationFixture],
};

export const evidenceFixture: EvidenceDetailView = {
  content_redacted: '[redacted log record]',
  provenance: {
    evidence_ref_id: TEST_EVIDENCE_ID,
    incident_id: TEST_INVESTIGATION_ID,
    service_id: TEST_SERVICE_ID,
    target_id: TEST_TARGET_ID,
    created_at: '2026-08-26T00:00:00Z',
    evidence_kind: 'log_record',
    source_kind: 'file',
    severity: 'error',
    log_cursor: 'snap-1',
  },
};

export const targetsFixture: TargetView[] = [
  {
    target_id: TEST_TARGET_ID,
    name: 'host-a',
    host: 'host-a.internal',
    authentication_configured: true,
    authentication_hint: 'key',
    host_key_policy: 'strict',
    optional_source_path: null,
    pinned_host_key_sha256: null,
    ssh_port: 22,
    ssh_user: 'admin',
    created_at: '2026-08-26T00:00:00Z',
    updated_at: '2026-08-26T00:00:00Z',
    version: 1,
  },
];

export const targetServicesFixture: TargetServiceView[] = [
  {
    service: TEST_SERVICE_ID,
    container_names: ['web-1', 'web-2'],
    allowed_host_paths: [],
    protected_remote_paths: [],
  },
  {
    service: TEST_SERVICE_ID_2,
    container_names: ['db-1'],
    allowed_host_paths: [],
    protected_remote_paths: [],
  },
];
