import { describe, expect, it, vi } from 'vitest';
import { createCommandRegistry, type CommandRegistry } from './registry.js';
import type { SlashCommand, CommandContext, CommandInvocation } from './types.js';

const emptyContext: CommandContext = {
  target: undefined,
  session: undefined,
  bootstrap: 'ready',
  capabilities: new Set<string>(),
};

const createMockCommand = (
  path: string[],
  options: Partial<SlashCommand> = {}
): SlashCommand => ({
  path,
  summary: `Mock command: ${path.join(' ')}`,
  group: 'system',
  usage: `/${path.join(' ')}`,
  dangerous: false,
  available: vi.fn().mockReturnValue(true),
  execute: vi.fn().mockResolvedValue({ kind: 'noop' }),
  ...options,
});

describe('CommandRegistry', () => {
  describe('registration', () => {
    it('registers commands with unique paths', () => {
      const commands = [
        createMockCommand(['help']),
        createMockCommand(['status']),
      ];
      const registry = createCommandRegistry(commands, emptyContext);

      expect(registry.commands).toHaveLength(2);
    });

    it('last registration wins for duplicate paths', () => {
      const cmd1 = createMockCommand(['help'], { summary: 'First' });
      const cmd2 = createMockCommand(['help'], { summary: 'Second' });
      const registry = createCommandRegistry([cmd1, cmd2], emptyContext);

      expect(registry.commands).toHaveLength(1);
      expect(registry.commands[0]?.summary).toBe('Second');
    });
  });

  describe('lookup', () => {
    it('finds command by exact path', () => {
      const cmd = createMockCommand(['target', 'add']);
      const registry = createCommandRegistry([cmd], emptyContext);

      const found = registry.find(['target', 'add']);
      expect(found).toBe(cmd);
    });

    it('finds longest matching prefix', () => {
      const cmdTarget = createMockCommand(['target']);
      const cmdTargetAdd = createMockCommand(['target', 'add']);
      const registry = createCommandRegistry([cmdTarget, cmdTargetAdd], emptyContext);

      const found = registry.find(['target', 'add']);
      expect(found).toBe(cmdTargetAdd);
    });

    it('returns undefined for unknown path', () => {
      const registry = createCommandRegistry([], emptyContext);

      expect(registry.find(['unknown'])).toBeUndefined();
    });
  });

  describe('context filtering', () => {
    it('filters commands by availability', () => {
      const available = createMockCommand(['help'], {
        available: vi.fn().mockReturnValue(true),
      });
      const unavailable = createMockCommand(['status'], {
        available: vi.fn().mockReturnValue(false),
      });

      const registry = createCommandRegistry([available, unavailable], emptyContext);
      const availableCommands = registry.getAvailable(emptyContext);

      expect(availableCommands).toHaveLength(1);
      expect(availableCommands[0]).toBe(available);
    });

    it('respects bootstrap state', () => {
      const readyOnly = createMockCommand(['help'], {
        available: vi.fn().mockImplementation((ctx: CommandContext) => ctx.bootstrap === 'ready'),
      });

      const loadingContext: CommandContext = { ...emptyContext, bootstrap: 'loading' };
      const registry = createCommandRegistry([readyOnly], loadingContext);

      expect(registry.getAvailable(loadingContext)).toHaveLength(0);
    });
  });

  describe('grouping', () => {
    it('groups commands by category', () => {
      const commands = [
        createMockCommand(['help'], { group: 'help' }),
        createMockCommand(['target'], { group: 'target' }),
        createMockCommand(['new'], { group: 'session' }),
        createMockCommand(['approve'], { group: 'approval' }),
      ];

      const registry = createCommandRegistry(commands, emptyContext);
      const groups = registry.getByGroup();

      expect(groups.get('help')).toHaveLength(1);
      expect(groups.get('target')).toHaveLength(1);
      expect(groups.get('session')).toHaveLength(1);
      expect(groups.get('approval')).toHaveLength(1);
    });
  });
});
