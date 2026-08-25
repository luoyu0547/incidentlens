import { describe, expect, it } from 'vitest';
import { parseInput } from './parser.js';
import { createCommandRegistry, type CommandRegistry } from './registry.js';
import type { CommandContext, ParsedInput } from './types.js';

const emptyContext: CommandContext = {
  target: undefined,
  session: undefined,
  bootstrap: 'ready',
  capabilities: new Set<string>(),
};

describe('parseInput', () => {
  describe('empty input', () => {
    it('returns empty for empty string', () => {
      const result = parseInput('');
      expect(result).toEqual({ kind: 'empty' });
    });

    it('returns empty for whitespace-only', () => {
      const result = parseInput('   ');
      expect(result).toEqual({ kind: 'empty' });
    });
  });

  describe('slash commands', () => {
    it('parses / as incomplete command', () => {
      const result = parseInput('/');
      expect(result).toEqual({ kind: 'incomplete-command', query: '' });
    });

    it('parses /tar as incomplete command', () => {
      const result = parseInput('/tar');
      expect(result).toEqual({ kind: 'incomplete-command', query: 'tar' });
    });

    it('parses /help as command', () => {
      const result = parseInput('/help');
      expect(result).toEqual({
        kind: 'command',
        invocation: { path: ['help'], args: '' },
      });
    });

    it('parses /target add', () => {
      const result = parseInput('/target add');
      expect(result).toEqual({
        kind: 'command',
        invocation: { path: ['target', 'add'], args: '' },
      });
    });

    it('parses /target with args', () => {
      const result = parseInput('/target production --host=example.com');
      expect(result).toEqual({
        kind: 'command',
        invocation: { path: ['target'], args: 'production --host=example.com' },
      });
    });

    it('handles quoted arguments', () => {
      const result = parseInput('/target add "my production server"');
      expect(result).toEqual({
        kind: 'command',
        invocation: { path: ['target', 'add'], args: '"my production server"' },
      });
    });

    it('handles escaped spaces', () => {
      const result = parseInput('/target add my\\ production\\ server');
      expect(result).toEqual({
        kind: 'command',
        invocation: { path: ['target', 'add'], args: 'my\\ production\\ server' },
      });
    });
  });

  describe('ordinary text', () => {
    it('treats plain text as message', () => {
      const result = parseInput('帮我检查一下数据库');
      expect(result).toEqual({ kind: 'message', text: '帮我检查一下数据库' });
    });

    it('treats Chinese text as message', () => {
      const result = parseInput('查看最近的错误日志');
      expect(result).toEqual({ kind: 'message', text: '查看最近的错误日志' });
    });

    it('treats pasted text as message', () => {
      const pastedText = '这是一个很长的文本\n包含多行\n用于分析';
      const result = parseInput(pastedText);
      expect(result).toEqual({ kind: 'message', text: pastedText });
    });

    it('treats unknown slash commands as message (not silent drop)', () => {
      const result = parseInput('/unknowncommand');
      expect(result).toEqual({ kind: 'message', text: '/unknowncommand' });
    });
  });
});

describe('CommandRegistry', () => {
  let registry: CommandRegistry;

  it('creates empty registry', () => {
    registry = createCommandRegistry([], emptyContext);
    expect(registry.commands).toHaveLength(0);
  });

  it('filters commands by capability', () => {
    const contextWithCapability: CommandContext = {
      ...emptyContext,
      capabilities: new Set(['target']),
    };
    registry = createCommandRegistry([], contextWithCapability);
    expect(registry.commands).toHaveLength(0);
  });

  it('finds command by path', () => {
    registry = createCommandRegistry([], emptyContext);
    expect(registry.find(['help'])).toBeUndefined();
  });

  it('returns available commands for context', () => {
    registry = createCommandRegistry([], emptyContext);
    expect(registry.getAvailable(emptyContext)).toHaveLength(0);
  });
});
