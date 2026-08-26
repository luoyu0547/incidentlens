import type { SlashCommand } from '../../commands/types.js';
import type { ApprovalDetailView } from '@incidentlens/protocol';
import type { ApprovalController } from './approval-controller.js';

export interface ApprovalCommandRuntime {
  readonly controller: ApprovalController;
  readonly listPending: () => Promise<readonly ApprovalDetailView[]>;
  readonly getCurrentApprovalId: () => string | undefined;
  readonly openReasonPrompt: (id: string, decision: 'approve' | 'reject') => void;
  readonly showDiff: (approval: ApprovalDetailView) => void;
}

function selectedId(args: string, runtime: ApprovalCommandRuntime): string | undefined {
  return args.trim() || runtime.getCurrentApprovalId();
}

/** CLI-only approval commands. They always refresh detail before an action. */
export function createApprovalCommands(runtime: ApprovalCommandRuntime): SlashCommand[] {
  const available = (ctx: Parameters<SlashCommand['available']>[0]) => ctx.bootstrap === 'ready';
  return [
    {
      path: ['approvals'], summary: 'List pending approvals', group: 'approval', usage: '/approvals',
      dangerous: false, available,
      execute: async () => {
        const approvals = await runtime.listPending();
        return approvals.length === 0
          ? { kind: 'message', text: 'No pending approvals.' }
          : { kind: 'message', text: `Pending approvals:\n${approvals.map((a) => `- ${a.approval_id}: ${a.intent_summary}`).join('\n')}` };
      },
    },
    ...(['approve', 'reject'] as const).map((decision) => ({
      path: [decision], summary: `${decision === 'approve' ? 'Approve' : 'Reject'} an approval`, group: 'approval' as const,
      usage: `/${decision} [approval-id]`, dangerous: true, available,
      execute: async (invocation: { args: string }) => {
        const id = selectedId(invocation.args, runtime);
        if (!id) return { kind: 'error' as const, message: `Usage: /${decision} <approval-id>` };
        const detail = await runtime.controller.refresh(id);
        if (detail.decision_status !== 'pending') return { kind: 'error' as const, message: `Approval ${id} is already ${detail.decision_status}.` };
        if (Date.parse(detail.expires_at) <= Date.now()) return { kind: 'error' as const, message: `Approval ${id} has expired.` };
        runtime.openReasonPrompt(id, decision);
        return { kind: 'message' as const, text: `Provide a reason to ${decision} approval ${id}.` };
      },
    })),
    {
      path: ['diff'], summary: 'Show the safe diff for an approval', group: 'approval', usage: '/diff [approval-id]',
      dangerous: false, available,
      execute: async (invocation) => {
        const id = selectedId(invocation.args, runtime);
        if (!id) return { kind: 'error', message: 'Usage: /diff <approval-id>' };
        const detail = await runtime.controller.refresh(id);
        runtime.showDiff(detail);
        return { kind: 'noop' };
      },
    },
  ];
}
