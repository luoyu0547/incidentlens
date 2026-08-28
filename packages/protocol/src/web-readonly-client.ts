// ---------------------------------------------------------------------------
// Read-only HTTP facade over the generated control-plane SDK.
//
// Web code (apps/web) must perform every product read through this facade.
// It is the only sanctioned HTTP surface for the observability UI: it always
// issues GET requests, never exposes the raw generated client or its endpoint
// functions, and normalizes failures into {@link ReadonlyApiError} without any
// headers, cookies, or credentials leaking through.
// ---------------------------------------------------------------------------

import { createClient } from './generated/client/index.js';
import {
  getEvidence,
  getInvestigationSummary,
  getIssue,
  getOverview,
  getService,
  listInvestigationSummaries,
  listIssues,
  listServiceLogs,
  listTargets,
  listTargetServices,
} from './generated/sdk.gen.js';
import type {
  EvidenceDetailView,
  InvestigationSummaryPage,
  InvestigationSummaryView,
  IssuePage,
  IssueView,
  ListInvestigationSummariesData,
  ListIssuesData,
  ListServiceLogsData,
  LogPage,
  OverviewView,
  ServiceDetailView,
  TargetServiceView,
  TargetView,
} from './generated/types.gen.js';

/**
 * The control plane API root mounted on the same origin. The generated SDK
 * endpoint paths already embed this prefix, so at this default the SDK client
 * stays origin-relative and request URLs remain `/api/v1/...` without the
 * prefix being doubled.
 */
export const DEFAULT_BASE_URL = '/api/v1';

// The browser facade may be exercised from a jsdom realm while the generated
// SDK's Request implementation comes from Node/undici. The read-only surface
// does not need to abort in-flight snapshots, so omit the signal at this
// boundary and avoid passing a cross-realm AbortSignal to Request.
function webSafeSignal(_signal?: AbortSignal): undefined {
  return undefined;
}

/**
 * Reduced page shape returned by {@link WebReadonlyClient.listTargets}. The
 * generated `ListTargetsResponse` is a bare array, so the "page" is the array
 * itself.
 */
export type TargetPage = TargetView[];

/**
 * Reduced page shape returned by {@link WebReadonlyClient.listTargetServices}.
 * The generated `ListTargetServicesResponse` is a bare array, so the "page" is
 * the array itself.
 */
export type TargetServicePage = TargetServiceView[];

/**
 * Cursor/filter query for {@link WebReadonlyClient.getServiceLogs}. Passed to
 * the generated SDK verbatim — nothing is parsed, reordered, or reassembled.
 */
export type ServiceLogQuery = NonNullable<ListServiceLogsData['query']>;

/**
 * Cursor/filter query for {@link WebReadonlyClient.listIssues}. Passed to the
 * generated SDK verbatim — nothing is parsed, reordered, or reassembled.
 */
export type IssueListQuery = NonNullable<ListIssuesData['query']>;

/**
 * Cursor/filter query for {@link WebReadonlyClient.listInvestigations}. Passed
 * to the generated SDK verbatim — nothing is parsed, reordered, or reassembled.
 */
export type InvestigationListQuery = NonNullable<ListInvestigationSummariesData['query']>;

/**
 * Options accepted by {@link createWebReadonlyClient}.
 */
export interface WebReadonlyClientOptions {
  /**
   * API root mounted on the same origin. Defaults to {@link DEFAULT_BASE_URL}
   * (`/api/v1`).
   */
  readonly baseUrl?: string;
  /**
   * Fetch implementation used for every request. Web code routes this through
   * the read-only guard; tests inject a stub. Defaults to `globalThis.fetch`.
   */
  readonly fetch?: typeof globalThis.fetch;
}

/**
 * Read-only facade over the generated control-plane SDK.
 *
 * Every method performs one GET. Mutations are neither declared nor reachable
 * through this interface. Failures reject with {@link ReadonlyApiError};
 * user-initiated aborts reject with the underlying `AbortError`.
 */
export interface WebReadonlyClient {
  getOverview(signal?: AbortSignal): Promise<OverviewView>;
  listTargets(signal?: AbortSignal): Promise<TargetPage>;
  listTargetServices(targetId: string, signal?: AbortSignal): Promise<TargetServicePage>;
  getService(serviceId: string, signal?: AbortSignal): Promise<ServiceDetailView>;
  getServiceLogs(serviceId: string, query: ServiceLogQuery, signal?: AbortSignal): Promise<LogPage>;
  listIssues(query: IssueListQuery, signal?: AbortSignal): Promise<IssuePage>;
  getIssue(issueId: string, signal?: AbortSignal): Promise<IssueView>;
  listInvestigations(query: InvestigationListQuery, signal?: AbortSignal): Promise<InvestigationSummaryPage>;
  getInvestigationSummary(id: string, signal?: AbortSignal): Promise<InvestigationSummaryView>;
  getEvidence(id: string, signal?: AbortSignal): Promise<EvidenceDetailView>;
}

/**
 * Options for {@link ReadonlyApiError}.
 */
export interface ReadonlyApiErrorOptions {
  readonly status?: number;
  readonly code?: string;
  readonly requestId?: string;
}

/**
 * Normalized read-only request failure.
 *
 * Deliberately carries only a message, numeric status, error code, and request
 * id — never response headers, cookies, or any credential material.
 */
export class ReadonlyApiError extends Error {
  readonly status?: number;
  readonly code?: string;
  readonly requestId?: string;

  constructor(message: string, options: ReadonlyApiErrorOptions = {}) {
    super(message);
    this.name = 'ReadonlyApiError';
    this.status = options.status;
    this.code = options.code;
    this.requestId = options.requestId;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function defaultMessage(status: number | undefined): string {
  return status === undefined
    ? 'The read-only request failed before a response was received'
    : `The read-only request failed with status ${status}`;
}

/**
 * Normalize a generated-SDK error into {@link ReadonlyApiError}. Only status,
 * code, message, and request id survive; headers, cookies, and credentials are
 * never attached. Aborts are rethrown unchanged so callers can distinguish
 * cancellation from failure.
 */
function toReadonlyApiError(value: unknown, status?: number): ReadonlyApiError {
  if (value instanceof ReadonlyApiError) {
    return value;
  }
  if (value instanceof DOMException && value.name === 'AbortError') {
    throw value;
  }
  if (value instanceof Error && value.name === 'AbortError') {
    throw value;
  }
  if (isRecord(value)) {
    const envelope = isRecord(value.error) ? value.error : value;
    const message = typeof envelope.message === 'string' ? envelope.message : defaultMessage(status);
    const code = typeof envelope.code === 'string' ? envelope.code : undefined;
    const requestId = typeof envelope.request_id === 'string' ? envelope.request_id : undefined;
    return new ReadonlyApiError(message, { status, code, requestId });
  }
  if (value instanceof Error) {
    return new ReadonlyApiError(value.message, { status });
  }
  return new ReadonlyApiError(typeof value === 'string' ? value : defaultMessage(status), { status });
}

/**
 * The generated SDK (throwOnError: false) resolves to either a data-bearing or
 * an error-bearing result. This is the minimal shared shape the facade unwraps.
 */
type SdkResult<T> =
  | { data: T; error: undefined; response?: Response }
  | { data: undefined; error: unknown; response?: Response };

function unwrap<T>(result: SdkResult<T>): T {
  if (result.error !== undefined) {
    throw toReadonlyApiError(result.error, result.response?.status);
  }
  return result.data as T;
}

/**
 * The generated SDK endpoint paths already begin with `/api/v1`. The facade's
 * {@link WebReadonlyClientOptions.baseUrl} names that same root, so the SDK
 * client receives only the origin portion — empty at the default, which keeps
 * request URLs same-origin relative (`/api/v1/...`) rather than absolute.
 */
function toSdkBaseUrl(baseUrl: string): string {
  // Strip trailing slash so prefix checks and slicing work cleanly
  const normalized = baseUrl.replace(/\/+$/, '');
  if (normalized === DEFAULT_BASE_URL || normalized === '') {
    return '';
  }
  return normalized.endsWith(DEFAULT_BASE_URL)
    ? normalized.slice(0, -DEFAULT_BASE_URL.length)
    : normalized;
}

/**
 * Create the read-only HTTP facade for the observability web client.
 *
 * @param options - base URL (defaults to same-origin `/api/v1`) and an
 * optional fetch implementation.
 */
export function createWebReadonlyClient(options?: WebReadonlyClientOptions): WebReadonlyClient {
  const sdk = createClient({
    baseUrl: toSdkBaseUrl(options?.baseUrl ?? DEFAULT_BASE_URL),
    ...(options?.fetch !== undefined ? { fetch: options.fetch } : {}),
    throwOnError: false,
  });

  return {
    getOverview: async (signal?: AbortSignal): Promise<OverviewView> =>
      unwrap<OverviewView>(await getOverview({ client: sdk, signal: webSafeSignal(signal) })),
    listTargets: async (signal?: AbortSignal): Promise<TargetPage> =>
      unwrap<TargetPage>(await listTargets({ client: sdk, signal: webSafeSignal(signal) })),
    listTargetServices: async (targetId: string, signal?: AbortSignal): Promise<TargetServicePage> =>
      unwrap<TargetServicePage>(await listTargetServices({ client: sdk, path: { target_id: targetId }, signal: webSafeSignal(signal) })),
    getService: async (serviceId: string, signal?: AbortSignal): Promise<ServiceDetailView> =>
      unwrap<ServiceDetailView>(await getService({ client: sdk, path: { service_id: serviceId }, signal: webSafeSignal(signal) })),
    getServiceLogs: async (
      serviceId: string,
      query: ServiceLogQuery,
      signal?: AbortSignal,
    ): Promise<LogPage> =>
      unwrap<LogPage>(await listServiceLogs({ client: sdk, path: { service_id: serviceId }, query, signal: webSafeSignal(signal) })),
    listIssues: async (query: IssueListQuery, signal?: AbortSignal): Promise<IssuePage> =>
      unwrap<IssuePage>(await listIssues({ client: sdk, query, signal: webSafeSignal(signal) })),
    getIssue: async (issueId: string, signal?: AbortSignal): Promise<IssueView> =>
      unwrap<IssueView>(await getIssue({ client: sdk, path: { issue_id: issueId }, signal: webSafeSignal(signal) })),
    listInvestigations: async (
      query: InvestigationListQuery,
      signal?: AbortSignal,
    ): Promise<InvestigationSummaryPage> =>
      unwrap<InvestigationSummaryPage>(
        await listInvestigationSummaries({ client: sdk, query, signal: webSafeSignal(signal) }),
      ),
    getInvestigationSummary: async (id: string, signal?: AbortSignal): Promise<InvestigationSummaryView> =>
      unwrap<InvestigationSummaryView>(
        await getInvestigationSummary({ client: sdk, path: { investigation_id: id }, signal: webSafeSignal(signal) }),
      ),
    getEvidence: async (id: string, signal?: AbortSignal): Promise<EvidenceDetailView> =>
      unwrap<EvidenceDetailView>(await getEvidence({ client: sdk, path: { evidence_ref_id: id }, signal: webSafeSignal(signal) })),
  };
}
