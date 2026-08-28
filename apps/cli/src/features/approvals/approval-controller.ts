import { createIdempotencyKey } from '../../api/idempotency.js';
import type { ControlPlaneApi } from '../../api/control-plane-api.js';
import type { ApprovalDetailView } from '@incidentlens/protocol';

/** Server-authoritative approval operations. */
export interface ApprovalController {
  refresh(id: string): Promise<ApprovalDetailView>;
  decide(id: string, decision: 'approve' | 'reject'): Promise<ApprovalDetailView>;
}

export interface ApprovalControllerOptions {
  readonly api: ControlPlaneApi;
}

/**
 * Approval mutations are deliberately thin: the returned server detail is the
 * only decision state callers may render. This controller never infers a
 * successful decision from a submitted request.
 */
export class ApprovalControllerImpl implements ApprovalController {
  private readonly api: ControlPlaneApi;

  constructor({ api }: ApprovalControllerOptions) {
    this.api = api;
  }

  refresh(id: string): Promise<ApprovalDetailView> {
    return this.api.getApproval(id);
  }

  async decide(id: string, decision: 'approve' | 'reject'): Promise<ApprovalDetailView> {
    return this.api.decideApproval(id, decision, {}, {
      idempotencyKey: createIdempotencyKey(),
    });
  }
}
