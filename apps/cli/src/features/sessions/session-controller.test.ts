/**
 * Session controller + command flow tests.
 *
 * Covers the session lifecycle from the task brief:
 * - no-target blocking for natural language
 * - create-on-first-message then subsequent messages in the same session
 * - operation tracking
 * - `/new`, `/sessions`, `/resume`, `/rename`, explicit `/cancel`,
 *   `/exit` no cancellation
 * - message retry idempotency (same key reused)
 * - restoration of the last session/target
 */

import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/api-error.js';
import type { ControlPlaneApi } from '../../api/control-plane-api.js';
import { createCommandRegistry } from '../../commands/registry.js';
import type {
  CommandContext,
  SlashCommand,
} from '../../commands/types.js';
import type { ConfigStore } from '../../config/types.js';
import type { CliAction } from '../../state/cli-state.js';
import type {
  AgentMessageAccepted,
  AgentSessionView,
  OperationAccepted,
  OperationView,
  TargetView,
} from '@incidentlens/protocol';
import { SessionController, type SessionControllerOptions } from './session-controller.js';
import { createSessionCommands, type SessionCommandRuntime } from './session-commands.js';

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

function makeSession(overrides: Partial<AgentSessionView> = {}): AgentSessionView {
  return {
    session_id: 'session-1',
    title: 'Production incident',
    status: 'idle',
    target_id: 'target-1',
    service_id: null,
    investigation_id: null,
    owner: 'user@example.com',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

const messageAccepted: AgentMessageAccepted = {
  accepted: true,
  message_id: 'msg-1',
  operation_id: 'op-1',
};

const operationAccepted: OperationAccepted = {
  accepted: true,
  operation_id: 'op-1',
};

const existingProfile = {
  profileName: 'default',
  apiUrl: 'https://api.example.com',
  lastSequenceBySession: {},
};

function makeOperation(overrides: Partial<OperationView> = {}): OperationView {
  return {
    operation_id: 'op-1',
    kind: 'agent_message',
    target_id: 'target-1',
    session_id: 'session-1',
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
  const createSession = vi.fn();
  const patchSession = vi.fn();
  const listSessions = vi.fn();
  const getSession = vi.fn();
  const listMessages = vi.fn();
  const sendMessage = vi.fn();
  const resumeSession = vi.fn();
  const cancelSession = vi.fn();
  const getOperation = vi.fn();
  const api = {
    createSession,
    patchSession,
    listSessions,
    getSession,
    listMessages,
    sendMessage,
    resumeSession,
    cancelSession,
    getOperation,
  } as unknown as ControlPlaneApi;
  return {
    api,
    createSession,
    patchSession,
    listSessions,
    getSession,
    listMessages,
    sendMessage,
    resumeSession,
    cancelSession,
    getOperation,
  };
}

function makeConfigStore() {
  const load = vi.fn();
  const save = vi.fn();
  const configStore = { load, save } as unknown as ConfigStore;
  return { configStore, load, save };
}

function makeOptions(
  api: ControlPlaneApi,
  configStore: ConfigStore,
  dispatch?: (action: CliAction) => void,
  onOperationProgress?: (operation: OperationView) => void,
): SessionControllerOptions {
  return { api, configStore, profileName: 'default', dispatch, onOperationProgress };
}

function makeSessionRuntime(
  controller: SessionController,
  overrides: Partial<SessionCommandRuntime> = {},
): SessionCommandRuntime {
  return {
    controller,
    status: vi.fn(),
    error: vi.fn(),
    openPicker: vi.fn(),
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
  session: makeSession(),
  bootstrap: 'ready',
  capabilities: new Set<string>(),
};

const readyNoTarget: CommandContext = {
  ...readyContext,
  target: undefined,
};

const readyNoSession: CommandContext = {
  ...readyContext,
  session: undefined,
};

const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

function verify(condition: boolean, message: string): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

/* ------------------------------------------------------------------ */
/* SessionController                                                   */
/* ------------------------------------------------------------------ */

describe('SessionController', () => {
  describe('no-target blocking', () => {
    it('throws before send when no target is selected', async () => {
      const { api, sendMessage } = makeApi();
      const { configStore } = makeConfigStore();
      const controller = new SessionController(makeOptions(api, configStore));
      controller.sync(undefined, undefined);

      await expect(controller.sendNaturalLanguage('hello')).rejects.toThrow(
        'No target selected',
      );
      expect(sendMessage).not.toHaveBeenCalled();
    });
  });

  describe('create-on-first-message', () => {
    it('creates a session then enqueues the first message on it', async () => {
      const { api, createSession, sendMessage } = makeApi();
      const session = makeSession();
      createSession.mockResolvedValue(session);
      sendMessage.mockResolvedValue(messageAccepted);
      const { configStore, load } = makeConfigStore();
      load.mockResolvedValue(existingProfile);
      const dispatch = vi.fn();
      const controller = new SessionController(makeOptions(api, configStore, dispatch));
      controller.sync(sampleTarget, undefined);

      const accepted = await controller.sendNaturalLanguage('investigate the spike');

      expect(accepted).toEqual(messageAccepted);
      expect(createSession).toHaveBeenCalledWith(
        { target_id: 'target-1' },
        { idempotencyKey: expect.any(String) },
      );
      expect(sendMessage).toHaveBeenCalledWith(
        'session-1',
        { content: 'investigate the spike' },
        { idempotencyKey: expect.any(String) },
      );
      expect(dispatch).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'set_session', session }),
      );
    });

    it('reuses the active session for subsequent messages in the same session', async () => {
      const { api, createSession, sendMessage } = makeApi();
      const firstSession = makeSession();
      createSession.mockResolvedValue(firstSession);
      sendMessage.mockResolvedValue(messageAccepted);
      const { configStore, load } = makeConfigStore();
      load.mockResolvedValue(existingProfile);
      const controller = new SessionController(makeOptions(api, configStore));
      controller.sync(sampleTarget, undefined);

      await controller.sendNaturalLanguage('first');
      await controller.sendNaturalLanguage('second');

      // create only happened once; both sends target the same session id.
      expect(createSession).toHaveBeenCalledTimes(1);
      expect(sendMessage).toHaveBeenCalledTimes(2);
      for (const call of sendMessage.mock.calls as unknown as [string, { content: string }][]) {
        expect(call[0]).toBe('session-1');
      }
    });
  });

  describe('message retry idempotency', () => {
    it('reuses the same idempotency key when a send fails then is retried', async () => {
      const { api, sendMessage } = makeApi();
      const session = makeSession();
      sendMessage
        .mockRejectedValueOnce(
          new ApiError({ message: 'upstream unavailable', code: 'unavailable', status: 503 }),
        )
        .mockResolvedValueOnce(messageAccepted);
      const { configStore } = makeConfigStore();
      const controller = new SessionController(makeOptions(api, configStore));
      controller.sync(sampleTarget, session);

      await expect(controller.sendNaturalLanguage('retry me')).rejects.toBeInstanceOf(ApiError);
      await expect(controller.sendNaturalLanguage('retry me')).resolves.toEqual(messageAccepted);

      verify(sendMessage.mock.calls[0] !== undefined, 'first send missing');
      verify(sendMessage.mock.calls[1] !== undefined, 'second send missing');
      const firstKey = (sendMessage.mock.calls[0][2] as { idempotencyKey: string }).idempotencyKey;
      const secondKey = (sendMessage.mock.calls[1][2] as { idempotencyKey: string }).idempotencyKey;
      expect(firstKey).toBeDefined();
      expect(firstKey).toBe(secondKey);
    });

    it('generates a fresh key per new send by default', async () => {
      const { api, createSession, sendMessage } = makeApi();
      const session = makeSession();
      createSession.mockResolvedValue(session);
      sendMessage.mockResolvedValue(messageAccepted);
      const { configStore, load } = makeConfigStore();
      load.mockResolvedValue(existingProfile);
      const controller = new SessionController(makeOptions(api, configStore));
      controller.sync(sampleTarget, undefined);

      await controller.sendNaturalLanguage('first');
      await controller.sendNaturalLanguage('second');

      const firstKey = (sendMessage.mock.calls[0]?.[2] as { idempotencyKey: string }).idempotencyKey;
      const secondKey = (sendMessage.mock.calls[1]?.[2] as { idempotencyKey: string }).idempotencyKey;
      expect(firstKey).toBeDefined();
      expect(secondKey).toBeDefined();
      expect(firstKey).not.toBe(secondKey);
    });
  });

  describe('session persistence', () => {
    it('persists lastSessionId into the profile on select', async () => {
      const { api } = makeApi();
      const { configStore, load, save } = makeConfigStore();
      load.mockResolvedValue(existingProfile);
      const dispatch = vi.fn();
      const controller = new SessionController(makeOptions(api, configStore, dispatch));
      const session = makeSession();

      await controller.select(session);

      expect(dispatch).toHaveBeenCalledWith({ type: 'set_session', session });
      expect(save).toHaveBeenCalledWith(
        expect.objectContaining({ lastSessionId: 'session-1', profileName: 'default' }),
      );
    });

    it('does not persist when no profile exists yet', async () => {
      const { api } = makeApi();
      const { configStore, load, save } = makeConfigStore();
      load.mockResolvedValue(null);
      const controller = new SessionController(makeOptions(api, configStore));

      await controller.select(makeSession());

      expect(save).not.toHaveBeenCalled();
    });
  });

  describe('rename', () => {
    it('renames the active session via PATCH', async () => {
      const { api, patchSession } = makeApi();
      const renamed = makeSession({ title: 'Renamed incident' });
      patchSession.mockResolvedValue(renamed);
      const { configStore } = makeConfigStore();
      const dispatch = vi.fn();
      const controller = new SessionController(makeOptions(api, configStore, dispatch));
      controller.sync(sampleTarget, makeSession());

      await expect(controller.rename('Renamed incident')).resolves.toEqual(renamed);
      expect(patchSession).toHaveBeenCalledWith(
        'session-1',
        { title: 'Renamed incident' },
        { idempotencyKey: expect.any(String) },
      );
      expect(dispatch).toHaveBeenCalledWith({ type: 'set_session', session: renamed });
    });

    it('throws when no active session exists', async () => {
      const { api } = makeApi();
      const controller = new SessionController(makeOptions(api, makeConfigStore().configStore));
      controller.sync(sampleTarget, undefined);

      await expect(controller.rename('abc')).rejects.toThrow('No active session');
    });
  });

  describe('operation tracking', () => {
    it('tracks the message operation and dispatches progress into state', async () => {
      const { api, sendMessage, getOperation } = makeApi();
      const session = makeSession();
      sendMessage.mockResolvedValue(messageAccepted);
      const succeeded = makeOperation({
        status: 'succeeded',
        progress_summary: 'Investigation complete',
      });
      getOperation.mockResolvedValue(succeeded);
      const { configStore, load } = makeConfigStore();
      load.mockResolvedValue(existingProfile);
      const onOperationProgress = vi.fn();
      const controller = new SessionController(
        makeOptions(api, configStore, undefined, onOperationProgress),
      );
      controller.sync(sampleTarget, session);

      await controller.sendNaturalLanguage('hello');

      // The tracker was started with the accepted operation id and must
      // have polled getOperation at least once.
      expect(sendMessage).toHaveBeenCalledWith(
        'session-1',
        { content: 'hello' },
        expect.any(Object),
      );
      expect(getOperation).toHaveBeenCalledWith('op-1', undefined);
      await flush();
      expect(onOperationProgress).toHaveBeenCalledWith(succeeded);
    });

    it('tracks the resume operation and reports progress', async () => {
      const { api, resumeSession, getSession, getOperation } = makeApi();
      resumeSession.mockResolvedValue(operationAccepted);
      getSession.mockResolvedValue(makeSession());
      const recovered = makeOperation({
        status: 'succeeded',
        progress_summary: 'Recovered',
      });
      getOperation.mockResolvedValue(recovered);
      const { configStore, load } = makeConfigStore();
      load.mockResolvedValue(existingProfile);
      const onOperationProgress = vi.fn();
      const controller = new SessionController(
        makeOptions(api, configStore, undefined, onOperationProgress),
      );
      controller.sync(sampleTarget, makeSession());

      await controller.resume('session-1');

      expect(getOperation).toHaveBeenCalledWith('op-1', undefined);
      await flush();
      expect(onOperationProgress).toHaveBeenCalledWith(recovered);
    });
  });

  describe('explicit /cancel is the only cancel path', () => {
    it('cancels the active session via the cancel API', async () => {
      const { api, cancelSession } = makeApi();
      const cancelled = makeOperation({ status: 'cancelled' });
      cancelSession.mockResolvedValue(cancelled);
      const { configStore } = makeConfigStore();
      const controller = new SessionController(makeOptions(api, configStore));
      controller.sync(sampleTarget, makeSession());

      await expect(controller.cancelCurrent()).resolves.toEqual(cancelled);
      expect(cancelSession).toHaveBeenCalledWith(
        'session-1',
        { idempotencyKey: expect.any(String) },
      );
    });

    it('does not call cancelSession during a normal send or resume', async () => {
      const { api, sendMessage, resumeSession, getSession, cancelSession } = makeApi();
      sendMessage.mockResolvedValue(messageAccepted);
      resumeSession.mockResolvedValue(operationAccepted);
      getSession.mockResolvedValue(makeSession());
      const { configStore, load } = makeConfigStore();
      load.mockResolvedValue(existingProfile);
      const controller = new SessionController(makeOptions(api, configStore));

      controller.sync(sampleTarget, makeSession());
      await controller.sendNaturalLanguage('hello');
      await controller.resume('session-1');

      expect(cancelSession).not.toHaveBeenCalled();
    });

    it('throws when there is no active session to cancel', async () => {
      const { api } = makeApi();
      const controller = new SessionController(makeOptions(api, makeConfigStore().configStore));
      controller.sync(sampleTarget, undefined);

      await expect(controller.cancelCurrent()).rejects.toThrow('No active session');
    });
  });
});

/* ------------------------------------------------------------------ */
/* SessionCommands                                                     */
/* ------------------------------------------------------------------ */

describe('SessionCommands', () => {
  it('exposes the session group through the command registry', () => {
    const { api } = makeApi();
    const controller = new SessionController(makeOptions(api, makeConfigStore().configStore));
    const registry = createCommandRegistry(
      createSessionCommands(makeSessionRuntime(controller)),
      readyContext,
    );

    const paths = registry.commands.map((c) => c.path.join('/'));
    expect(paths).toEqual(['new', 'sessions', 'resume', 'rename', 'cancel']);
    expect(registry.getByGroup().get('session')).toHaveLength(5);
  });

  it('/new creates a session and reports the id', async () => {
    const { api, createSession } = makeApi();
    createSession.mockResolvedValue(makeSession());
    const controller = new SessionController(makeOptions(api, makeConfigStore().configStore));
    controller.sync(sampleTarget, undefined);
    const status = vi.fn();
    const commands = createSessionCommands(makeSessionRuntime(controller, { status }));
    const cmd = findCommand(commands, ['new']);

    const result = await cmd.execute({ path: ['new'], args: '' }, readyNoSession);

    verify(result.kind === 'message', 'expected message result');
    expect(result.text).toContain('session-1');
    expect(status).toHaveBeenCalledWith(expect.stringContaining('session-1'));
    expect(createSession).toHaveBeenCalledWith(
      { target_id: 'target-1', title: null },
      { idempotencyKey: expect.any(String) },
    );
  });

  it('/new is not available without a target', () => {
    const { api } = makeApi();
    const controller = new SessionController(makeOptions(api, makeConfigStore().configStore));
    const commands = createSessionCommands(makeSessionRuntime(controller));

    expect(findCommand(commands, ['new']).available(readyNoTarget)).toBe(false);
    expect(findCommand(commands, ['new']).available(readyNoSession)).toBe(true);
  });

  it('/sessions without args opens the picker', async () => {
    const { api } = makeApi();
    const controller = new SessionController(makeOptions(api, makeConfigStore().configStore));
    const openPicker = vi.fn();
    const commands = createSessionCommands(makeSessionRuntime(controller, { openPicker }));
    const cmd = findCommand(commands, ['sessions']);

    const result = await cmd.execute({ path: ['sessions'], args: '' }, readyContext);

    verify(result.kind === 'message', 'expected message result');
    expect(openPicker).toHaveBeenCalled();
  });

  it('/sessions <id> selects a session by id', async () => {
    const { api, getSession } = makeApi();
    const session = makeSession();
    getSession.mockResolvedValue(session);
    const { configStore, load, save } = makeConfigStore();
    load.mockResolvedValue(existingProfile);
    const dispatch = vi.fn();
    const controller = new SessionController(makeOptions(api, configStore, dispatch));
    const commands = createSessionCommands(makeSessionRuntime(controller));
    const cmd = findCommand(commands, ['sessions']);

    const result = await cmd.execute({ path: ['sessions'], args: 'session-1' }, readyContext);

    verify(result.kind === 'message', 'expected message result');
    expect(getSession).toHaveBeenCalledWith('session-1');
    expect(dispatch).toHaveBeenCalledWith({ type: 'set_session', session });
    expect(save).toHaveBeenCalledWith(expect.objectContaining({ lastSessionId: 'session-1' }));
  });

  it('/resume attaches and requests server-side recovery, returning an operation', async () => {
    const { api, resumeSession, getSession } = makeApi();
    const session = makeSession({ status: 'paused' });
    resumeSession.mockResolvedValue(operationAccepted);
    getSession.mockResolvedValue(session);
    const controller = new SessionController(makeOptions(api, makeConfigStore().configStore));
    const status = vi.fn();
    const commands = createSessionCommands(makeSessionRuntime(controller, { status }));
    const cmd = findCommand(commands, ['resume']);

    const result = await cmd.execute({ path: ['resume'], args: 'session-1' }, readyContext);

    verify(result.kind === 'message', 'expected message result');
    expect(resumeSession).toHaveBeenCalledWith('session-1', {
      idempotencyKey: expect.any(String),
    });
    expect(getSession).toHaveBeenCalledWith('session-1');
    expect(status).toHaveBeenCalledWith(expect.stringContaining('op-1'));
  });

  it('/resume reports a missing id', async () => {
    const { api } = makeApi();
    const controller = new SessionController(makeOptions(api, makeConfigStore().configStore));
    const commands = createSessionCommands(makeSessionRuntime(controller));
    const cmd = findCommand(commands, ['resume']);

    const result = await cmd.execute({ path: ['resume'], args: '' }, readyContext);

    expect(result.kind).toBe('error');
  });

  it('/rename renames the active session', async () => {
    const { api, patchSession } = makeApi();
    const renamed = makeSession({ title: 'New title' });
    patchSession.mockResolvedValue(renamed);
    const controller = new SessionController(makeOptions(api, makeConfigStore().configStore));
    controller.sync(sampleTarget, makeSession());
    const status = vi.fn();
    const commands = createSessionCommands(makeSessionRuntime(controller, { status }));
    const cmd = findCommand(commands, ['rename']);

    const result = await cmd.execute({ path: ['rename'], args: 'New title' }, readyContext);

    verify(result.kind === 'message', 'expected message result');
    expect(patchSession).toHaveBeenCalledWith('session-1', { title: 'New title' }, expect.any(Object));
    expect(status).toHaveBeenCalledWith(expect.stringContaining('New title'));
  });

  it('/rename is gated on an active session', () => {
    const { api } = makeApi();
    const controller = new SessionController(makeOptions(api, makeConfigStore().configStore));
    const commands = createSessionCommands(makeSessionRuntime(controller));

    expect(findCommand(commands, ['rename']).available(readyNoSession)).toBe(false);
    expect(findCommand(commands, ['rename']).available(readyContext)).toBe(true);
  });

  it('/cancel calls the cancel API explicitly and reports the result', async () => {
    const { api, cancelSession } = makeApi();
    const cancelled = makeOperation({ status: 'cancelled' });
    cancelSession.mockResolvedValue(cancelled);
    const controller = new SessionController(makeOptions(api, makeConfigStore().configStore));
    controller.sync(sampleTarget, makeSession());
    const status = vi.fn();
    const commands = createSessionCommands(makeSessionRuntime(controller, { status }));
    const cmd = findCommand(commands, ['cancel']);

    const result = await cmd.execute({ path: ['cancel'], args: '' }, readyContext);

    verify(result.kind === 'message', 'expected message result');
    expect(cancelSession).toHaveBeenCalledWith('session-1', {
      idempotencyKey: expect.any(String),
    });
    expect(status).toHaveBeenCalledWith(expect.stringContaining('cancelled'));
  });

  it('never calls the cancel API for /new, /sessions, /resume, or /rename', async () => {
    const { api, cancelSession, createSession, getSession, patchSession, resumeSession } =
      makeApi();
    createSession.mockResolvedValue(makeSession());
    getSession.mockResolvedValue(makeSession());
    patchSession.mockResolvedValue(makeSession());
    resumeSession.mockResolvedValue(operationAccepted);
    const controller = new SessionController(makeOptions(api, makeConfigStore().configStore));
    controller.sync(sampleTarget, makeSession());
    const commands = createSessionCommands(makeSessionRuntime(controller));

    await findCommand(commands, ['new']).execute({ path: ['new'], args: '' }, readyNoSession);
    await findCommand(commands, ['sessions']).execute({ path: ['sessions'], args: 'session-1' }, readyContext);
    await findCommand(commands, ['resume']).execute({ path: ['resume'], args: 'session-1' }, readyContext);
    await findCommand(commands, ['rename']).execute({ path: ['rename'], args: 'Title' }, readyContext);

    expect(cancelSession).not.toHaveBeenCalled();
  });
});