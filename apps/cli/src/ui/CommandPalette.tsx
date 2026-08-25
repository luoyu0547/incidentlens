/**
 * Command palette component for IncidentLens CLI.
 *
 * Renders a filterable list of available slash commands with
 * keyboard navigation support.
 */

import React, { useMemo } from 'react';
import { Box, Text } from 'ink';
import type { SlashCommand } from '../commands/types.js';

interface CommandPaletteProps {
  readonly query: string;
  readonly commands: readonly SlashCommand[];
  readonly selectedIndex: number;
  readonly onSelect: (command: SlashCommand) => void;
  readonly onCancel: () => void;
  readonly focused?: boolean;
}

/**
 * Group label colors by command group.
 */
const GROUP_COLORS: Record<string, string> = {
  help: 'cyan',
  target: 'green',
  connection: 'yellow',
  session: 'blue',
  scope: 'magenta',
  investigation: 'white',
  approval: 'red',
  system: 'gray',
};

/**
 * Command palette component.
 * Shows filtered commands based on user query.
 */
export function CommandPalette({
  query,
  commands,
  selectedIndex,
  onSelect: _onSelect,
  onCancel: _onCancel,
  focused = false,
}: CommandPaletteProps): React.ReactElement | null {
  // Filter commands by query
  const filteredCommands = useMemo(() => {
    if (!focused && query === '') {
      return [];
    }

    if (query === '') {
      return commands;
    }

    const lowerQuery = query.toLowerCase();
    return commands.filter((cmd) => {
      const cmdStr = `/${cmd.path.join(' ')}`.toLowerCase();
      return cmdStr.includes(lowerQuery);
    });
  }, [query, commands, focused]);

  // Group commands
  const groupedCommands = useMemo(() => {
    const groups = new Map<string, SlashCommand[]>();

    for (const cmd of filteredCommands) {
      const group = groups.get(cmd.group) ?? [];
      group.push(cmd);
      groups.set(cmd.group, group);
    }

    return groups;
  }, [filteredCommands]);

  if (filteredCommands.length === 0) {
    return null;
  }

  let globalIndex = 0;

  return (
    <Box flexDirection="column">
      {Array.from(groupedCommands.entries()).map(([group, cmds]) => (
        <Box key={group} flexDirection="column">
          <Text color={GROUP_COLORS[group] ?? 'white'} bold>
            {group.toUpperCase()}
          </Text>
          {cmds.map((cmd) => {
            const isSelected = globalIndex === selectedIndex;
            const idx = globalIndex;
            globalIndex++;

            return (
              <Box key={cmd.path.join('/')} paddingLeft={2}>
                <Text color={isSelected ? 'blue' : undefined} inverse={isSelected}>
                  {isSelected ? '>' : ' '}
                  {` /${cmd.path.join(' ')}`}
                </Text>
                <Text color="gray">{`  ${cmd.summary}`}</Text>
                {cmd.dangerous && <Text color="red"> !</Text>}
              </Box>
            );
          })}
        </Box>
      ))}

      {/* Usage hint for selected command */}
      {selectedIndex >= 0 && selectedIndex < filteredCommands.length && (
        <Box marginTop={1}>
          <Text color="gray">
            Usage: {filteredCommands[selectedIndex]?.usage}
          </Text>
        </Box>
      )}
    </Box>
  );
}
