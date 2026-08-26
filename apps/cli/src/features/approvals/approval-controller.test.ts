import { describe, expect, it, vi } from 'vitest';
import { ApprovalControllerImpl } from './approval-controller.js';
import { createApprovalCommands } from './approval-commands.js';
import type { ControlPlaneApi } from '../../api/control-plane-api.js';
import type { ApprovalDetailView } from '@incidentlens/protocol';

const approval = (overrides: Partial<ApprovalDetailView> = {}): ApprovalDetailView => ({
  approval_id: 'approval-1', created_at: '2026-08-26T00:00:00Z', decision_status: 'pending',
  downstream_status: 'pending', expires_at: '2099-08-27T00:00:00Z', intent_summary: 'Restart safely',
  kind: 'service_restart', linkage: {}, risk: 'medium', status: 'pending', ...overrides,
});

describe('ApprovalControllerImpl', () => {
  it('refreshes an approval with the authoritative detail endpoint', async () => {
    const detail = approval();
    const api = { getApproval: vi.fn().mockResolvedValue(detail) } as unknown as ControlPlaneApi;
    await expect(new ApprovalControllerImpl({ api }).refresh('approval-1')).resolves.toBe(detail);
    expect(api.getApproval).toHaveBeenCalledWith('approval-1');
  });

  it('requires a non-whitespace reason before mutating', async () => {
    const api = { decideApproval: vi.fn() } as unknown as ControlPlaneApi;
    await expect(new ApprovalControllerImpl({ api }).decide('approval-1', 'approve', '  ')).rejects.toThrow('A reason is required');
    expect(api.decideApproval).not.toHaveBeenCalled();
  });

  it('returns only the server-persisted result and does not manufacture success', async () => {
    const persisted = approval({ decision_status: 'approved', status: 'approved', downstream_status: 'processing' });
    const api = { decideApproval: vi.fn().mockResolvedValue(persisted) } as unknown as ControlPlaneApi;
    await expect(new ApprovalControllerImpl({ api }).decide('approval-1', 'approve', 'validated impact')).resolves.toBe(persisted);
    expect(api.decideApproval).toHaveBeenCalledWith('approval-1', 'approve', { reason: 'validated impact' }, expect.objectContaining({ idempotencyKey: expect.any(String) }));
  });
});

describe('approval commands', () => {
  it('refreshes the authoritative detail before opening a required-reason decision prompt', async () => {
    const detail = approval();
    const controller = { refresh: vi.fn().mockResolvedValue(detail), decide: vi.fn() };
    const openReasonPrompt = vi.fn();
    const commands = createApprovalCommands({ controller, listPending: vi.fn(), getCurrentApprovalId: () => 'approval-1', openReasonPrompt, showDiff: vi.fn() });
    const approve = commands.find((command) => command.path.join(' ') === 'approve');
    if (!approve) throw new Error('approve command missing');
    await approve.execute({ path: ['approve'], args: '' }, { bootstrap: 'ready', target: undefined, session: undefined, capabilities: new Set() });
    expect(controller.refresh).toHaveBeenCalledWith('approval-1');
    expect(openReasonPrompt).toHaveBeenCalledWith('approval-1', 'approve');
  });

  it('blocks expired and duplicate decisions after the authoritative GET', async () => {
    const controller = { refresh: vi.fn().mockResolvedValue(approval({ decision_status: 'approved' })), decide: vi.fn() };
    const commands = createApprovalCommands({ controller, listPending: vi.fn(), getCurrentApprovalId: () => 'approval-1', openReasonPrompt: vi.fn(), showDiff: vi.fn() });
    const approve = commands.find((command) => command.path.join(' ') === 'approve');
    if (!approve) throw new Error('approve command missing');
    await expect(approve.execute({ path: ['approve'], args: '' }, { bootstrap: 'ready', target: undefined, session: undefined, capabilities: new Set() })).resolves.toMatchObject({ kind: 'error' });
  });
});
