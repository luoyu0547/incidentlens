import { describe, expect, it, vi } from 'vitest';
import { render } from 'ink-testing-library';
import React from 'react';
import { CommandPalette } from './CommandPalette.js';
import type { SlashCommand, CommandContext } from '../commands/types.js';

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

describe('CommandPalette', () => {
  it('renders empty when no query and no commands', () => {
    const { lastFrame } = render(
      <CommandPalette
        query=""
        commands={[]}
        selectedIndex={0}
        onSelect={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    expect(lastFrame()).toBe('');
  });

  it('shows filtered commands when query is provided', () => {
    const commands = [
      createMockCommand(['help']),
      createMockCommand(['target']),
      createMockCommand(['new']),
    ];

    const { lastFrame } = render(
      <CommandPalette
        query="tar"
        commands={commands}
        selectedIndex={0}
        onSelect={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    const output = lastFrame();
    expect(output).toContain('/target');
    expect(output).not.toContain('/help');
    expect(output).not.toContain('/new');
  });

  it('shows all commands when query is empty but has focus', () => {
    const commands = [
      createMockCommand(['help']),
      createMockCommand(['target']),
    ];

    const { lastFrame } = render(
      <CommandPalette
        query=""
        commands={commands}
        selectedIndex={0}
        onSelect={vi.fn()}
        onCancel={vi.fn()}
        focused={true}
      />
    );

    const output = lastFrame();
    expect(output).toContain('/help');
    expect(output).toContain('/target');
  });

  it('highlights selected command', () => {
    const commands = [
      createMockCommand(['help']),
      createMockCommand(['target']),
    ];

    const { lastFrame } = render(
      <CommandPalette
        query=""
        commands={commands}
        selectedIndex={1}
        onSelect={vi.fn()}
        onCancel={vi.fn()}
        focused={true}
      />
    );

    const output = lastFrame();
    expect(output).toContain('>');
  });

  it('shows command summary', () => {
    const commands = [
      createMockCommand(['help'], { summary: 'Show available commands' }),
    ];

    const { lastFrame } = render(
      <CommandPalette
        query="help"
        commands={commands}
        selectedIndex={0}
        onSelect={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    const output = lastFrame();
    expect(output).toContain('Show available commands');
  });

  it('groups commands by category', () => {
    const commands = [
      createMockCommand(['help'], { group: 'help' }),
      createMockCommand(['target'], { group: 'target' }),
      createMockCommand(['new'], { group: 'session' }),
    ];

    const { lastFrame } = render(
      <CommandPalette
        query=""
        commands={commands}
        selectedIndex={0}
        onSelect={vi.fn()}
        onCancel={vi.fn()}
        focused={true}
      />
    );

    const output = lastFrame();
    expect(output).toContain('HELP');
    expect(output).toContain('TARGET');
    expect(output).toContain('SESSION');
  });

  it('shows usage for selected command', () => {
    const commands = [
      createMockCommand(['target', 'add'], { usage: '/target add <name>' }),
    ];

    const { lastFrame } = render(
      <CommandPalette
        query="target"
        commands={commands}
        selectedIndex={0}
        onSelect={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    const output = lastFrame();
    expect(output).toContain('/target add <name>');
  });

  it('marks dangerous commands', () => {
    const commands = [
      createMockCommand(['target', 'remove'], { dangerous: true }),
    ];

    const { lastFrame } = render(
      <CommandPalette
        query="target"
        commands={commands}
        selectedIndex={0}
        onSelect={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    const output = lastFrame();
    expect(output).toContain('!');
  });
});
