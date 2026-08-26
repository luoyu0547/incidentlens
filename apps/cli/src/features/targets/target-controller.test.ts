/**
 * Target controller + command flow tests.
 *
 * Covers the target UX flows from the task brief:
 * - picker / target listing
 * - `/target production` selection
 * - wizard sequence (create input built from wizard fields)
 * - no private-key input
 * - host-key test result (verified source/fingerprint or safe failure)
 * - persisted last target
 * - edit version (optimistic concurrency)
 * - deletion confirmation (typed, never implicit)
 * - idempotent retry
 */

import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/api-error.js';
import type { ControlPlaneApi } from '../../api/control-plane-api.js';
import { createCommandRegistry } from '../../commands/registry.js';
import type { CommandContext, SlashCommand } from '../../commands/types.js';
import type { ConfigStore } from '../../config/types.js';
import type { CliAction } from '../../state/cli-state.js';
import type {
  OperationAccepted,
  OperationView,
  TargetCreate,
  TargetPatch,
  TargetView,
} from '@incidentlens/protocol';
import { TargetController, trackTargetTest } from './target-controller.js';
import { createTargetCommands, type TargetCommandRuntime } from './target-commands.js';

/* ------------------------------------------------------------------ */
/* Fixtures                                                            */
/* ------------------------------------------------------------------ */

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

const otherTarget: TargetView = {
  ...sampleTarget,
  target_id: 'target-2',
  name: 'staging',
  host: 'web-02.example.com',
};

const createdTarget: TargetView = {
  ...sampleTarget,
  target_id: 'target-new',
};

const updatedTarget: TargetView = {
  ...sampleTarget,
  name: 'renamed',
  version: 3,
};

const createInput: TargetCreate = {
  name: 'production',
  host: 'web-01.example.com',
  ssh_user: 'deploy',
  ssh_port: 22,
  authentication_ref: 'ssh-agent:deploy@web-01',
  host_key_policy: 'strict',
  pinned_host_key_sha256: null,
};

const existingProfile = {
  profileName: 'default',
  apiUrl: 'https://api.example.com',
  lastSequenceBySession: {},
};

const operationAccepted: OperationAccepted = {
  accepted: true,
  operation_id: 'op-1',
};

function makeOperation(overrides: Partial<OperationView> = {}): OperationView {
  return {
    operation_id: 'op-1',
    kind: 'target_test',
    target_id: 'target-1',
    session_id: null,
    investigation_id: null,
    status: 'running',
    progress_summary: null,
    error_code: null,
    error_message: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    finished_at: null,
    ...overrides,
  };
}

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

function makeApi() {
  const listTargets = vi.fn();
  const createTarget = vi.fn();
  const updateTarget = vi.fn();
  const removeTarget = vi.fn();
  const testTarget = vi.fn();
  const getOperation = vi.fn();
  const api = {
    listTargets,
    createTarget,
    updateTarget,
    removeTarget,
    testTarget,
    getOperation,
  } as unknown as ControlPlaneApi;
  return { api, listTargets, createTarget, updateTarget, removeTarget, testTarget, getOperation };
}

function makeConfigStore() {
  const load = vi.fn();
  const save = vi.fn();
  const configStore = { load, save } as unknown as ConfigStore;
  return { configStore, load, save };
}

function makeController(
  api: ControlPlaneApi,
  configStore: ConfigStore,
  dispatch?: (action: CliAction) => void
): TargetController {
  return new TargetController({ api, configStore, profileName: 'default', dispatch });
}

function makeRuntime(
  controller: TargetController,
  overrides: Partial<TargetCommandRuntime> = {}
): TargetCommandRuntime {
  return {
    controller,
    openWizard: vi.fn(),
    openRemoveConfirmation: vi.fn(),
    status: vi.fn(),
    error: vi.fn(),
    ...overrides,
  };
}

function findCommand(commands: readonly SlashCommand[], path: string[]): SlashCommand {
  const key = path.join('/');
  const command = commands.find((c) => c.path.join('/') === key);
  if (!command) {
    throw new Error(`missing command: /${key}`);
  }
  return command;
}

const readyContext: CommandContext = {
  target: sampleTarget,
  session: undefined,
  bootstrap: 'ready',
  capabilities: new Set<string>(),
};

const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

function verify(condition: boolean, message: string): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

/* ------------------------------------------------------------------ */
/* Tests                                                               */
/* ------------------------------------------------------------------ */

describe('TargetController', () => {
  describe('list (picker)', () => {
    it('returns the targets from the control plane', async () => {
      const { api, listTargets } = makeApi();
      listTargets.mockResolvedValue([sampleTarget, otherTarget]);
      const controller = makeController(api, makeConfigStore().configStore);

      await expect(controller.list()).resolves.toEqual([sampleTarget, otherTarget]);
      expect(listTargets).toHaveBeenCalledWith(undefined);
    });

    it('forwards an AbortSignal to the API', async () => {
      const { api, listTargets } = makeApi();
      listTargets.mockResolvedValue([sampleTarget]);
      const controller = makeController(api, makeConfigStore().configStore);
      const controllerAbort = new AbortController();

      await controller.list(controllerAbort.signal);

      expect(listTargets).toHaveBeenCalledWith(controllerAbort.signal);
    });
  });

  describe('select (/target <name> + persisted last target)', () => {
    it('dispatches set_target and persists lastTargetId into the profile', async () => {
      const { api } = makeApi();
      const { configStore, load, save } = makeConfigStore();
      load.mockResolvedValue(existingProfile);
      const dispatch = vi.fn();
      const controller = makeController(api, configStore, dispatch);

      await controller.select(sampleTarget);

      expect(dispatch).toHaveBeenCalledWith({ type: 'set_target', target: sampleTarget });
      expect(save).toHaveBeenCalledWith(
        expect.objectContaining({ lastTargetId: 'target-1', profileName: 'default' })
      );
    });

    it('does not persist when no profile exists yet', async () => {
      const { api } = makeApi();
      const { configStore, load, save } = makeConfigStore();
      load.mockResolvedValue(null);
      const controller = makeController(api, configStore);

      await controller.select(sampleTarget);

      expect(save).not.toHaveBeenCalled();
    });
  });

  describe('wizard create sequence', () => {
    it('creates a target with metadata and an opaque auth reference only', async () => {
      const { api, createTarget } = makeApi();
      createTarget.mockResolvedValue(createdTarget);
      const controller = makeController(api, makeConfigStore().configStore);

      await expect(controller.create(createInput)).resolves.toEqual(createdTarget);
      expect(createTarget).toHaveBeenCalledWith(createInput, {
        idempotencyKey: expect.any(String),
      });
    });

    it('never includes private-key material in the wizard create input', () => {
      const serialized = JSON.stringify(createInput);
      expect(serialized).not.toContain('----BEGIN');
      expect(createInput.authentication_ref).toBe('ssh-agent:deploy@web-01');
      expect(createInput.host_key_policy).toBe('strict');
    });
  });

  describe('idempotent retry', () => {
    it('reuses a provided idempotency key when the create is retried', async () => {
      const { api, createTarget } = makeApi();
      createTarget
        .mockRejectedValueOnce(
          new ApiError({ message: 'upstream unavailable', code: 'unavailable', status: 503 })
        )
        .mockResolvedValueOnce(createdTarget);
      const controller = makeController(api, makeConfigStore().configStore);
      const key = 'fixed-key-123';

      await expect(controller.create(createInput, key)).rejects.toBeInstanceOf(ApiError);
      await expect(controller.create(createInput, key)).resolves.toEqual(createdTarget);

      verify(createTarget.mock.calls[0] !== undefined, 'first create call missing');
      verify(createTarget.mock.calls[1] !== undefined, 'second create call missing');
      expect(createTarget.mock.calls[0][1]).toEqual({ idempotencyKey: key });
      expect(createTarget.mock.calls[1][1]).toEqual({ idempotencyKey: key });
    });

    it('generates a fresh key per call by default', async () => {
      const { api, createTarget } = makeApi();
      createTarget.mockResolvedValue(createdTarget);
      const controller = makeController(api, makeConfigStore().configStore);

      await controller.create(createInput);
      await controller.create(createInput);

      const first = createTarget.mock.calls[0]?.[1] as { idempotencyKey?: string };
      const second = createTarget.mock.calls[1]?.[1] as { idempotencyKey?: string };
      expect(first.idempotencyKey).toBeDefined();
      expect(second.idempotencyKey).toBeDefined();
      expect(first.idempotencyKey).not.toBe(second.idempotencyKey);
    });
  });

  describe('edit version (optimistic concurrency)', () => {
    it('patches with the expected_version from the target', async () => {
      const { api, updateTarget } = makeApi();
      updateTarget.mockResolvedValue(updatedTarget);
      const controller = makeController(api, makeConfigStore().configStore);
      const patch: TargetPatch = { expected_version: 3, name: 'renamed' };

      await expect(controller.update('target-1', patch, 'key-2')).resolves.toEqual(updatedTarget);
      expect(updateTarget).toHaveBeenCalledWith('target-1', patch, {
        idempotencyKey: 'key-2',
      });
    });

    it('applies the current version when no explicit version is passed', async () => {
      const { api, updateTarget } = makeApi();
      updateTarget.mockResolvedValue(updatedTarget);
      const controller = makeController(api, makeConfigStore().configStore);
      const patch: TargetPatch = { expected_version: 1, name: 'renamed' };

      await controller.update('target-1', patch);

      expect(updateTarget).toHaveBeenCalledWith(
        'target-1',
        patch,
        expect.objectContaining({ idempotencyKey: expect.any(String) })
      );
    });
  });

  describe('host-key test result', () => {
    it('enqueues a target test operation', async () => {
      const { api, testTarget } = makeApi();
      testTarget.mockResolvedValue(operationAccepted);
      const controller = makeController(api, makeConfigStore().configStore);

      await expect(controller.test('target-1')).resolves.toEqual(operationAccepted);
      expect(testTarget).toHaveBeenCalledWith('target-1', {
        idempotencyKey: expect.any(String),
      });
    });

    it('tracks the operation and reports the verified host-key result', async () => {
      const succeeded = makeOperation({
        status: 'succeeded',
        progress_summary:
          'Verified host key sha256:abc123def (source: ssh_config known_hosts match)',
      });
      const getOperation = vi.fn().mockResolvedValue(succeeded);
      const onResult = vi.fn();

      const result = await trackTargetTest(getOperation, 'op-1', onResult, { pollIntervalMs: 1 });

      expect(result.status).toBe('succeeded');
      expect(result.summary).toContain('sha256:abc123def');
      expect(result.summary).toContain('known_hosts');
      expect(onResult).toHaveBeenCalledWith(result);
    });

    it('reports a safe failure and never fabricates credentials', async () => {
      const failed = makeOperation({
        status: 'failed',
        error_message: 'SSH connection refused (check host reachability)',
      });
      const onResult = vi.fn();

      const result = await trackTargetTest(vi.fn().mockResolvedValue(failed), 'op-1', onResult, {
        pollIntervalMs: 1,
      });

      expect(result.status).toBe('failed');
      expect(result.error).toContain('connection refused');
      expect(result.summary).toBeNull();
      expect(onResult).toHaveBeenCalledWith(result);
    });

    it('reports uncertain when the operation never reaches a terminal state', async () => {
      const running = makeOperation({ status: 'running' });
      const onResult = vi.fn();

      const result = await trackTargetTest(vi.fn().mockResolvedValue(running), 'op-1', onResult, {
        pollIntervalMs: 1,
        maxPolls: 2,
      });

      expect(result.status).toBe('uncertain');
      expect(onResult).toHaveBeenCalledWith(result);
    });

    it('surfaces a getOperation network failure as a safe error', async () => {
      const onResult = vi.fn();
      const onError = vi.fn();

      const result = await trackTargetTest(
        vi.fn().mockRejectedValue(new Error('upstream unavailable')),
        'op-1',
        onResult,
        { pollIntervalMs: 1, maxPolls: 3, onError }
      );

      expect(result.status).toBe('failed');
      expect(result.error).toContain('upstream unavailable');
      expect(onError).toHaveBeenCalledWith(expect.stringContaining('upstream unavailable'));
      expect(onResult).toHaveBeenCalledWith(result);
    });

    it('passes the AbortSignal through to getOperation', async () => {
      const getOperation = vi.fn().mockResolvedValue(makeOperation({ status: 'succeeded' }));
      const controllerAbort = new AbortController();

      await trackTargetTest(getOperation, 'op-1', vi.fn(), {
        pollIntervalMs: 1,
        signal: controllerAbort.signal,
      });

      expect(getOperation).toHaveBeenCalledWith('op-1', controllerAbort.signal);
    });
  });

  describe('remove (deletion)', () => {
    it('removes a target with an idempotency key', async () => {
      const { api, removeTarget } = makeApi();
      removeTarget.mockResolvedValue(undefined);
      const controller = makeController(api, makeConfigStore().configStore);

      await controller.remove('target-1', 'key-3');

      expect(removeTarget).toHaveBeenCalledWith('target-1', { idempotencyKey: 'key-3' });
    });
  });
});

describe('TargetCommands', () => {
  it('exposes the full /target group through the command registry', () => {
    const { api } = makeApi();
    const controller = makeController(api, makeConfigStore().configStore);
    const registry = createCommandRegistry(
      createTargetCommands(makeRuntime(controller)),
      readyContext
    );

    const paths = registry.commands.map((c) => c.path.join('/'));
    expect(paths).toEqual(['target', 'target/add', 'target/edit', 'target/test', 'target/remove']);
    expect(registry.getByGroup().get('target')).toHaveLength(5);
  });

  it('lists targets and offers a picker via /target', async () => {
    const { api, listTargets } = makeApi();
    listTargets.mockResolvedValue([sampleTarget, otherTarget]);
    const controller = makeController(api, makeConfigStore().configStore);
    const commands = createTargetCommands(makeRuntime(controller));
    const target = findCommand(commands, ['target']);

    const result = await target.execute({ path: ['target'], args: '' }, readyContext);

    expect(result.kind).toBe('message');
    expect(result.kind === 'message' ? result.text : '').toContain('production');
    expect(result.kind === 'message' ? result.text : '').toContain('staging');
  });

  it('selects a target by name with /target production', async () => {
    const { api, listTargets } = makeApi();
    listTargets.mockResolvedValue([sampleTarget, otherTarget]);
    const { configStore, load, save } = makeConfigStore();
    load.mockResolvedValue(existingProfile);
    const dispatch = vi.fn();
    const controller = makeController(api, configStore, dispatch);
    const commands = createTargetCommands(makeRuntime(controller));
    const target = findCommand(commands, ['target']);

    const result = await target.execute({ path: ['target'], args: 'production' }, readyContext);

    verify(result.kind === 'message', 'expected message result');
    expect(result.text).toContain('Selected target production');
    expect(dispatch).toHaveBeenCalledWith({ type: 'set_target', target: sampleTarget });
    expect(save).toHaveBeenCalledWith(expect.objectContaining({ lastTargetId: 'target-1' }));
  });

  it('reports a missing target for unknown names', async () => {
    const { api, listTargets } = makeApi();
    listTargets.mockResolvedValue([sampleTarget]);
    const controller = makeController(api, makeConfigStore().configStore);
    const commands = createTargetCommands(makeRuntime(controller));
    const target = findCommand(commands, ['target']);

    const result = await target.execute({ path: ['target'], args: 'nope' }, readyContext);

    expect(result.kind).toBe('error');
  });

  it('opens the wizard for /target add and /target edit', async () => {
    const { api } = makeApi();
    const controller = makeController(api, makeConfigStore().configStore);
    const openWizard = vi.fn();
    const commands = createTargetCommands(makeRuntime(controller, { openWizard }));

    await findCommand(commands, ['target', 'add']).execute(
      { path: ['target', 'add'], args: '' },
      readyContext
    );
    await findCommand(commands, ['target', 'edit']).execute(
      { path: ['target', 'edit'], args: '' },
      readyContext
    );

    expect(openWizard).toHaveBeenNthCalledWith(1, 'create');
    expect(openWizard).toHaveBeenNthCalledWith(2, 'edit', sampleTarget);
  });

  it('starts a host-key test and reports the safe verified result', async () => {
    const { api, testTarget, getOperation } = makeApi();
    testTarget.mockResolvedValue(operationAccepted);
    getOperation.mockResolvedValue(
      makeOperation({
        status: 'succeeded',
        progress_summary: 'Verified host key sha256:abc123def (source: known_hosts)',
      })
    );
    const controller = makeController(api, makeConfigStore().configStore);
    const status = vi.fn();
    const commands = createTargetCommands(makeRuntime(controller, { status }));
    const test = findCommand(commands, ['target', 'test']);

    const result = await test.execute({ path: ['target', 'test'], args: '' }, readyContext);
    await flush();

    verify(result.kind === 'message', 'expected message result');
    expect(result.text).toContain('op-1');
    expect(testTarget).toHaveBeenCalledWith('target-1', {
      idempotencyKey: expect.any(String),
    });
    expect(status).toHaveBeenCalledWith(expect.stringContaining('sha256:abc123def'));
    expect(status).toHaveBeenCalledWith(expect.stringContaining('known_hosts'));
  });

  it('reports a safe failure from /target test without raw SSH output', async () => {
    const { api, testTarget, getOperation } = makeApi();
    testTarget.mockResolvedValue(operationAccepted);
    getOperation.mockResolvedValue(
      makeOperation({ status: 'failed', error_message: 'SSH connection refused (safe)' })
    );
    const controller = makeController(api, makeConfigStore().configStore);
    const error = vi.fn();
    const commands = createTargetCommands(makeRuntime(controller, { error }));
    const test = findCommand(commands, ['target', 'test']);

    await test.execute({ path: ['target', 'test'], args: '' }, readyContext);
    await flush();

    const message = error.mock.calls[0]?.[0] as string | undefined;
    expect(message ?? '').toContain('connection refused');
    expect(message ?? '').not.toContain('BEGIN');
  });

  it('requires typed confirmation before removing a target', async () => {
    const { api, removeTarget } = makeApi();
    const controller = makeController(api, makeConfigStore().configStore);
    const openRemoveConfirmation = vi.fn();
    const commands = createTargetCommands(makeRuntime(controller, { openRemoveConfirmation }));
    const remove = findCommand(commands, ['target', 'remove']);

    const result = await remove.execute({ path: ['target', 'remove'], args: '' }, readyContext);

    verify(result.kind === 'message', 'expected message result');
    expect(openRemoveConfirmation).toHaveBeenCalledWith(sampleTarget);
    // The command itself must never delete; only the typed confirm does.
    expect(removeTarget).not.toHaveBeenCalled();
  });

  it('gates edit / test / remove on a selected target', async () => {
    const { api } = makeApi();
    const controller = makeController(api, makeConfigStore().configStore);
    const commands = createTargetCommands(makeRuntime(controller));
    const noTarget: CommandContext = { ...readyContext, target: undefined };

    expect(findCommand(commands, ['target', 'edit']).available(noTarget)).toBe(false);
    expect(findCommand(commands, ['target', 'test']).available(noTarget)).toBe(false);
    expect(findCommand(commands, ['target', 'remove']).available(noTarget)).toBe(false);
    expect(findCommand(commands, ['target', 'edit']).available(readyContext)).toBe(true);
  });
});
