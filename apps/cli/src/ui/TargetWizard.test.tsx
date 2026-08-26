/**
 * Target wizard UI tests.
 *
 * Verifies the keyboard-driven create/edit flow, the security contract
 * (no private-key input, masked auth reference on review), stable
 * idempotency key across a failed submit retry, and the typed removal
 * confirmation overlay.
 *
 * Note: after `render()` the test settles briefly so Ink's `useInput`
 * passive effects have subscribed to stdin before the first write.
 */

import { describe, expect, it, vi } from 'vitest';
import { render } from 'ink-testing-library';
import React from 'react';
import type { TargetView } from '@incidentlens/protocol';
import { TargetController } from '../features/targets/target-controller.js';
import { RemoveTargetPrompt, TargetWizard } from './TargetWizard.js';

/**
 * The ink-testing-library render() instance type is not exported; derive
 * it from the render function.
 */
type Instance = ReturnType<typeof render>;

const sampleTarget: TargetView = {
  target_id: 'target-1',
  name: 'production',
  host: 'web-01.example.com',
  ssh_user: 'deploy',
  ssh_port: 22,
  authentication_configured: true,
  authentication_hint: 'ssh-agent:deploy@web-01',
  host_key_policy: 'strict',
  pinned_host_key_sha256: null,
  optional_source_path: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  version: 1,
};

const createdTarget: TargetView = {
  ...sampleTarget,
  target_id: 'target-new',
};

function makeController(overrides: Partial<Record<'create' | 'update', unknown>> = {}) {
  return {
    create: vi.fn().mockResolvedValue(createdTarget),
    update: vi.fn().mockResolvedValue(sampleTarget),
    ...overrides,
  } as unknown as TargetController;
}

const tick = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

/**
 * Wait long enough for Ink's useInput subscription effects to mount.
 */
const settle = async (): Promise<void> => {
  await new Promise((resolve) => setTimeout(resolve, 20));
};

async function typeText(instance: Instance, text: string): Promise<void> {
  instance.stdin.write(text);
  await tick();
}

async function pressEnter(instance: Instance): Promise<void> {
  instance.stdin.write('\r');
  await tick();
}

/**
 * Drive the create wizard through every field using default port and
 * strict host-key policy, leaving the state on the review step.
 */
async function driveToReview(instance: Instance, authRef: string): Promise<void> {
  await typeText(instance, 'production');
  await pressEnter(instance);

  await typeText(instance, 'web-01.example.com');
  await pressEnter(instance);

  await typeText(instance, 'deploy');
  await pressEnter(instance);

  // Port remains the default "22".
  await pressEnter(instance);

  // Auth reference (opaque) step.
  await typeText(instance, authRef);
  await pressEnter(instance);

  // Host-key policy: strict.
  instance.stdin.write('1');
  await tick();
  await pressEnter(instance);
}

describe('TargetWizard', () => {
  it('renders the first field on mount', () => {
    const { lastFrame } = render(
      <TargetWizard
        mode="create"
        controller={makeController()}
        onComplete={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    expect(lastFrame()).toContain('New Target');
    expect(lastFrame()).toContain('Target name');
  });

  it('completes the create sequence with metadata plus an opaque auth reference', async () => {
    const controller = makeController();
    const onComplete = vi.fn();
    const instance = render(
      <TargetWizard
        mode="create"
        controller={controller}
        onComplete={onComplete}
        onCancel={vi.fn()}
      />
    );
    await settle();

    await driveToReview(instance, 'ssh-agent:deploy@web-01');

    expect(instance.lastFrame()).toContain('Review');

    await pressEnter(instance);

    expect(controller.create).toHaveBeenCalledWith(
      expect.objectContaining({
        name: 'production',
        host: 'web-01.example.com',
        ssh_user: 'deploy',
        ssh_port: 22,
        authentication_ref: 'ssh-agent:deploy@web-01',
        host_key_policy: 'strict',
      }),
      expect.any(String)
    );
    expect(onComplete).toHaveBeenCalledWith(createdTarget);
  });

  it('never offers a private-key input at any step', async () => {
    const instance = render(
      <TargetWizard
        mode="create"
        controller={makeController()}
        onComplete={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    await settle();

    await driveToReview(instance, 'ssh-agent:deploy@web-01');

    const allFrames = instance.frames.join('\n');
    expect(allFrames).not.toContain('BEGIN');
    expect(allFrames).not.toContain('private key');
  });

  it('masks the authentication reference on the review step', async () => {
    const instance = render(
      <TargetWizard
        mode="create"
        controller={makeController()}
        onComplete={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    await settle();

    await driveToReview(instance, 'ssh-agent:deploy@web-01');

    const reviewFrame = instance.lastFrame();
    expect(reviewFrame).toContain('reference set');
    expect(reviewFrame).not.toContain('ssh-agent:deploy@web-01');
  });

  it('pre-fills edit mode from the current target', () => {
    const { lastFrame } = render(
      <TargetWizard
        mode="edit"
        target={sampleTarget}
        controller={makeController()}
        onComplete={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    const frame = lastFrame();
    expect(frame).toContain('Edit Target');
    expect(frame).toContain('production');
  });

  it('submits a patch with the expected version in edit mode', async () => {
    const controller = makeController();
    const onComplete = vi.fn();
    const instance = render(
      <TargetWizard
        mode="edit"
        target={sampleTarget}
        controller={controller}
        onComplete={onComplete}
        onCancel={vi.fn()}
      />
    );
    await settle();

    await pressEnter(instance); // keep pre-filled name
    await pressEnter(instance); // keep pre-filled host
    await pressEnter(instance); // keep pre-filled user
    await pressEnter(instance); // keep port 22
    await pressEnter(instance); // auth left empty → keep current
    instance.stdin.write('1');
    await tick();
    await pressEnter(instance); // strict policy → review
    await pressEnter(instance); // submit

    expect(controller.update).toHaveBeenCalledWith(
      'target-1',
      expect.objectContaining({
        expected_version: 1,
        name: 'production',
        host: 'web-01.example.com',
        ssh_user: 'deploy',
        ssh_port: 22,
        host_key_policy: 'strict',
        authentication_ref: null,
      }),
      expect.any(String)
    );
    expect(onComplete).toHaveBeenCalledWith(sampleTarget);
  });

  it('reuses the same idempotency key when resubmitting after a failure', async () => {
    const create = vi
      .fn()
      .mockRejectedValueOnce(new Error('network error'))
      .mockResolvedValueOnce(createdTarget);
    const controller = { create, update: vi.fn() } as unknown as TargetController;
    const instance = render(
      <TargetWizard mode="create" controller={controller} onComplete={vi.fn()} onCancel={vi.fn()} />
    );
    await settle();

    await driveToReview(instance, 'ssh-agent:deploy@web-01');

    // First submit fails.
    await pressEnter(instance);
    expect(instance.lastFrame()).toContain('network error');

    // Second submit succeeds with the same key.
    await pressEnter(instance);

    expect(create).toHaveBeenCalledTimes(2);
    const firstKey = create.mock.calls[0]?.[1];
    const secondKey = create.mock.calls[1]?.[1];
    expect(firstKey).toBeDefined();
    expect(firstKey).toBe(secondKey);
  });

  it('cancels on escape', async () => {
    const onCancel = vi.fn();
    const instance = render(
      <TargetWizard
        mode="create"
        controller={makeController()}
        onComplete={vi.fn()}
        onCancel={onCancel}
      />
    );
    await settle();

    instance.stdin.write('');
    await tick();

    expect(onCancel).toHaveBeenCalled();
  });
});

describe('RemoveTargetPrompt', () => {
  it('does not confirm until the exact target name is typed', async () => {
    const onConfirm = vi.fn();
    const instance = render(
      <RemoveTargetPrompt target={sampleTarget} onConfirm={onConfirm} onCancel={vi.fn()} />
    );
    await settle();

    await typeText(instance, 'staging');
    expect(instance.lastFrame()).toContain('Name does not match yet.');

    await pressEnter(instance);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('confirms once the exact target name is typed', async () => {
    const onConfirm = vi.fn();
    const instance = render(
      <RemoveTargetPrompt target={sampleTarget} onConfirm={onConfirm} onCancel={vi.fn()} />
    );
    await settle();

    await typeText(instance, 'production');
    expect(instance.lastFrame()).toContain('Press Enter to remove.');

    await pressEnter(instance);
    expect(onConfirm).toHaveBeenCalled();
  });

  it('cancels on escape', async () => {
    const onCancel = vi.fn();
    const instance = render(
      <RemoveTargetPrompt target={sampleTarget} onConfirm={vi.fn()} onCancel={onCancel} />
    );
    await settle();

    instance.stdin.write('');
    await tick();

    expect(onCancel).toHaveBeenCalled();
  });
});
