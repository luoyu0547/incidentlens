/**
 * API error class for typed error handling.
 * Normalizes error envelope from the server.
 */
export class ApiError extends Error {
  readonly code: string;
  readonly requestId?: string;
  readonly status?: number;
  readonly details: unknown;
  readonly retryable: boolean;

  constructor(options: {
    message: string;
    code: string;
    requestId?: string;
    status?: number;
    details?: unknown;
    retryable?: boolean;
  }) {
    super(options.message);
    this.name = 'ApiError';
    this.code = options.code;
    this.requestId = options.requestId;
    this.status = options.status;
    this.details = options.details;
    this.retryable = options.retryable ?? false;
  }
}
