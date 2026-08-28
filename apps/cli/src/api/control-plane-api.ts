import { ApiError } from './api-error.js';
import { IDEMPOTENCY_KEY_HEADER } from './idempotency.js';
import type {
  ApiVersionView,
  Principal,
  TargetView,
  TargetCreate,
  TargetPatch,
  OperationAccepted,
  AgentSessionView,
  AgentSessionCreate,
  AgentSessionPatch,
  AgentMessageView,
  AgentMessageAccepted,
  AgentMessageCreate,
  OperationView,
  ApprovalPage,
  ApprovalDetailView,
  ApprovalDecisionRequest,
  EventPage,
} from '@incidentlens/protocol';

/**
 * Options for mutation operations.
 */
export interface MutationOptions {
  readonly idempotencyKey: string;
  readonly signal?: AbortSignal;
}

/**
 * Query parameters for session listing.
 */
export interface AgentSessionListQuery {
  readonly limit?: number;
  readonly offset?: number;
}

/**
 * Query parameters for message listing.
 */
export interface MessageListQuery {
  readonly limit?: number;
  readonly offset?: number;
}

/**
 * Query parameters for approval listing.
 */
export interface ApprovalListQuery {
  readonly status?: string;
  readonly sessionId?: string;
  readonly limit?: number;
  readonly offset?: number;
}

/**
 * Query parameters for the product event log used to learn the
 * authoritative latest sequence for a session during gap recovery.
 */
export interface EventLogQuery {
  readonly sessionId?: string;
  readonly targetId?: string;
  readonly investigationId?: string;
  readonly afterSequence?: number;
  readonly limit?: number;
}

/**
 * Control plane API client interface.
 * Components should import this, never generated endpoint functions directly.
 */
export interface ControlPlaneApi {
  compatibility(signal?: AbortSignal): Promise<ApiVersionView>;
  principal(signal?: AbortSignal): Promise<Principal>;
  listTargets(signal?: AbortSignal): Promise<TargetView[]>;
  createTarget(input: TargetCreate, options: MutationOptions): Promise<TargetView>;
  updateTarget(id: string, input: TargetPatch, options: MutationOptions): Promise<TargetView>;
  removeTarget(id: string, options: MutationOptions): Promise<void>;
  testTarget(id: string, options: MutationOptions): Promise<OperationAccepted>;
  createSession(input: AgentSessionCreate, options: MutationOptions): Promise<AgentSessionView>;
  patchSession(id: string, input: AgentSessionPatch, options: MutationOptions): Promise<AgentSessionView>;
  listSessions(query: AgentSessionListQuery, signal?: AbortSignal): Promise<AgentSessionView[]>;
  getSession(id: string, signal?: AbortSignal): Promise<AgentSessionView>;
  listMessages(id: string, query: MessageListQuery, signal?: AbortSignal): Promise<AgentMessageView[]>;
  sendMessage(id: string, input: AgentMessageCreate, options: MutationOptions): Promise<AgentMessageAccepted>;
  resumeSession(id: string, options: MutationOptions): Promise<OperationAccepted>;
  cancelSession(id: string, options: MutationOptions): Promise<OperationView>;
  getOperation(id: string, signal?: AbortSignal): Promise<OperationView>;
  listApprovals(query: ApprovalListQuery, signal?: AbortSignal): Promise<ApprovalPage>;
  listEvents(query: EventLogQuery, signal?: AbortSignal): Promise<EventPage>;
  getApproval(id: string, signal?: AbortSignal): Promise<ApprovalDetailView>;
  decideApproval(
    id: string,
    decision: 'approve' | 'reject',
    input: ApprovalDecisionRequest,
    options: MutationOptions
  ): Promise<ApprovalDetailView>;
}

/**
 * Configuration for the ControlPlaneApi client.
 */
export interface ControlPlaneApiConfig {
  readonly baseUrl: string;
  readonly token?: string;
  readonly maxRetries?: number;
  readonly retryDelay?: number;
}

/**
 * HTTP methods for retry classification.
 */
type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

/**
 * Typed control plane HTTP client.
 * Wraps generated SDK with retry logic, error normalization, and idempotency.
 */
export class ControlPlaneApi implements ControlPlaneApi {
  private readonly baseUrl: string;
  private readonly token?: string;
  private readonly maxRetries: number;
  private readonly retryDelay: number;

  constructor(config: ControlPlaneApiConfig) {
    this.baseUrl = config.baseUrl.replace(/\/$/, '');
    this.token = config.token;
    this.maxRetries = config.maxRetries ?? 2;
    this.retryDelay = config.retryDelay ?? 1000;
  }

  async compatibility(signal?: AbortSignal): Promise<ApiVersionView> {
    return this.request<ApiVersionView>('GET', '/api/v1/version', undefined, signal);
  }

  async principal(signal?: AbortSignal): Promise<Principal> {
    return this.request<Principal>('GET', '/api/v1/principal', undefined, signal);
  }

  async listTargets(signal?: AbortSignal): Promise<TargetView[]> {
    return this.request<TargetView[]>('GET', '/api/v1/targets', undefined, signal);
  }

  async createTarget(input: TargetCreate, options: MutationOptions): Promise<TargetView> {
    return this.request<TargetView>('POST', '/api/v1/targets', input, options.signal, options.idempotencyKey);
  }

  async updateTarget(id: string, input: TargetPatch, options: MutationOptions): Promise<TargetView> {
    return this.request<TargetView>('PATCH', `/api/v1/targets/${id}`, input, options.signal, options.idempotencyKey);
  }

  async removeTarget(id: string, options: MutationOptions): Promise<void> {
    await this.request<void>('DELETE', `/api/v1/targets/${id}`, undefined, options.signal, options.idempotencyKey);
  }

  async testTarget(id: string, options: MutationOptions): Promise<OperationAccepted> {
    return this.request<OperationAccepted>('POST', `/api/v1/targets/${id}/test`, undefined, options.signal, options.idempotencyKey);
  }

  async createSession(input: AgentSessionCreate, options: MutationOptions): Promise<AgentSessionView> {
    return this.request<AgentSessionView>('POST', '/api/v1/agent-sessions', input, options.signal, options.idempotencyKey);
  }

  async patchSession(id: string, input: AgentSessionPatch, options: MutationOptions): Promise<AgentSessionView> {
    return this.request<AgentSessionView>('PATCH', `/api/v1/agent-sessions/${id}`, input, options.signal, options.idempotencyKey);
  }

  async listSessions(query: AgentSessionListQuery, signal?: AbortSignal): Promise<AgentSessionView[]> {
    const params = new URLSearchParams();
    if (query.limit !== undefined) params.set('limit', String(query.limit));
    if (query.offset !== undefined) params.set('offset', String(query.offset));
    const qs = params.toString();
    return this.request<AgentSessionView[]>('GET', `/api/v1/agent-sessions${qs ? `?${qs}` : ''}`, undefined, signal);
  }

  async getSession(id: string, signal?: AbortSignal): Promise<AgentSessionView> {
    return this.request<AgentSessionView>('GET', `/api/v1/agent-sessions/${id}`, undefined, signal);
  }

  async listMessages(id: string, query: MessageListQuery, signal?: AbortSignal): Promise<AgentMessageView[]> {
    const params = new URLSearchParams();
    if (query.limit !== undefined) params.set('limit', String(query.limit));
    if (query.offset !== undefined) params.set('offset', String(query.offset));
    const qs = params.toString();
    return this.request<AgentMessageView[]>('GET', `/api/v1/agent-sessions/${id}/messages${qs ? `?${qs}` : ''}`, undefined, signal);
  }

  async sendMessage(id: string, input: AgentMessageCreate, options: MutationOptions): Promise<AgentMessageAccepted> {
    return this.request<AgentMessageAccepted>('POST', `/api/v1/agent-sessions/${id}/messages`, input, options.signal, options.idempotencyKey);
  }

  async resumeSession(id: string, options: MutationOptions): Promise<OperationAccepted> {
    return this.request<OperationAccepted>('POST', `/api/v1/agent-sessions/${id}/resume`, undefined, options.signal, options.idempotencyKey);
  }

  async cancelSession(id: string, options: MutationOptions): Promise<OperationView> {
    return this.request<OperationView>('POST', `/api/v1/agent-sessions/${id}/cancel`, undefined, options.signal, options.idempotencyKey);
  }

  async getOperation(id: string, signal?: AbortSignal): Promise<OperationView> {
    return this.request<OperationView>('GET', `/api/v1/operations/${id}`, undefined, signal);
  }

  async listApprovals(query: ApprovalListQuery, signal?: AbortSignal): Promise<ApprovalPage> {
    const params = new URLSearchParams();
    if (query.status !== undefined) params.set('status', query.status);
    if (query.sessionId !== undefined) params.set('session_id', query.sessionId);
    if (query.limit !== undefined) params.set('limit', String(query.limit));
    if (query.offset !== undefined) params.set('offset', String(query.offset));
    const qs = params.toString();
    return this.request<ApprovalPage>('GET', `/api/v1/approvals${qs ? `?${qs}` : ''}`, undefined, signal);
  }

  async listEvents(query: EventLogQuery, signal?: AbortSignal): Promise<EventPage> {
    const params = new URLSearchParams();
    if (query.sessionId !== undefined) params.set('session_id', query.sessionId);
    if (query.targetId !== undefined) params.set('target_id', query.targetId);
    if (query.investigationId !== undefined) params.set('investigation_id', query.investigationId);
    if (query.afterSequence !== undefined) params.set('after_sequence', String(query.afterSequence));
    if (query.limit !== undefined) params.set('limit', String(query.limit));
    const qs = params.toString();
    return this.request<EventPage>('GET', `/api/v1/events${qs ? `?${qs}` : ''}`, undefined, signal);
  }

  async getApproval(id: string, signal?: AbortSignal): Promise<ApprovalDetailView> {
    return this.request<ApprovalDetailView>('GET', `/api/v1/approvals/${id}`, undefined, signal);
  }

  async decideApproval(
    id: string,
    decision: 'approve' | 'reject',
    input: ApprovalDecisionRequest,
    options: MutationOptions
  ): Promise<ApprovalDetailView> {
    const endpoint = decision === 'approve' ? 'approve' : 'reject';
    return this.request<ApprovalDetailView>('POST', `/api/v1/approvals/${id}/${endpoint}`, input, options.signal, options.idempotencyKey);
  }

  private async request<T>(
    method: HttpMethod,
    path: string,
    body?: unknown,
    signal?: AbortSignal,
    idempotencyKey?: string
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`;
    const isRetryable = method === 'GET';
    let lastError: Error | undefined;

    for (let attempt = 0; attempt <= (isRetryable ? this.maxRetries : 0); attempt++) {
      try {
        return await this.fetchWithRetry<T>(method, url, body, signal, idempotencyKey);
      } catch (error) {
        lastError = error as Error;

        // Don't retry AbortError
        if (error instanceof DOMException && error.name === 'AbortError') {
          throw error;
        }

        // Check if error is retryable
        if (isRetryable && this.isRetryableError(error) && attempt < this.maxRetries) {
          await this.delay(this.retryDelay * Math.pow(2, attempt));
          continue;
        }

        throw error;
      }
    }

    throw lastError;
  }

  private async fetchWithRetry<T>(
    method: HttpMethod,
    url: string,
    body?: unknown,
    signal?: AbortSignal,
    idempotencyKey?: string
  ): Promise<T> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };

    if (this.token) {
      headers['Authorization'] = `Bearer ${this.token}`;
    }

    if (idempotencyKey) {
      headers[IDEMPOTENCY_KEY_HEADER] = idempotencyKey;
    }

    const response = await fetch(url, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
      signal,
    });

    const requestId = response.headers.get('x-request-id') ?? undefined;

    if (!response.ok) {
      const errorBody = await response.json().catch(() => null);
      const error = this.createApiError(response.status, errorBody, requestId);
      throw error;
    }

    // Handle 204 No Content
    if (response.status === 204) {
      return undefined as T;
    }

    return response.json() as Promise<T>;
  }

  private createApiError(status: number, body: unknown, requestId?: string): ApiError {
    const errorBody = body as Record<string, unknown> | null;
    const message = (errorBody?.['message'] as string) ?? `Request failed with status ${status}`;
    const code = (errorBody?.['error'] as string) ?? 'unknown_error';
    const details = errorBody?.['details'] ?? errorBody;

    return new ApiError({
      message,
      code,
      requestId,
      status,
      details,
      retryable: this.isRetryableStatus(status),
    });
  }

  private isRetryableError(error: unknown): boolean {
    if (error instanceof TypeError && error.message === 'Failed to fetch') {
      return true;
    }
    if (error instanceof ApiError) {
      return error.retryable;
    }
    return false;
  }

  private isRetryableStatus(status: number): boolean {
    return status === 408 || status === 429 || (status >= 500 && status < 600);
  }

  private delay(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}
