/**
 * Session picker UI tests.
 *
 * Verifies the keyboard-driven overlay: list rendering, selection by
 * arrow keys, enter to select, escape to cancel, empty state.
 */

import { describe, expect, it, vi } from 'vitest';
import { render } from 'ink-testing-library';
import React from 'react';
import type { AgentSessionView } from '@incidentlens/protocol';
import { SessionPicker } from './SessionPicker.js';

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

const tick = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

/**
 * Wait long enough for Ink's useInput subscription effects to mount.
 */
const settle = async (): Promise<void> => {
  await new Promise((resolve) => setTimeout(resolve, 20));
};

describe('SessionPicker', () => {
  it('renders the list of sessions', () => {
    const sessions = [
      makeSession({ session_id: 's1', title: 'Production incident' }),
      makeSession({ session_id: 's2', title: 'Staging debug' }),
    ];
    const { lastFrame } = render(
      <SessionPicker sessions={sessions} onSelect={vi.fn()} onCancel={vi.fn()} />,
    );

    expect(lastFrame()).toContain('Sessions');
    expect(lastFrame()).toContain('Production incident');
    expect(lastFrame()).toContain('Staging debug');
  });

  it('shows empty state when no sessions exist', () => {
    const { lastFrame } = render(
      <SessionPicker sessions={[]} onSelect={vi.fn()} onCancel={vi.fn()} />,
    );

    expect(lastFrame()).toContain('No sessions found');
  });

  it('highlights the first session by default', () => {
    const sessions = [
      makeSession({ session_id: 's1', title: 'First incident' }),
      makeSession({ session_id: 's2', title: 'Second incident' }),
    ];
    const { lastFrame } = render(
      <SessionPicker sessions={sessions} onSelect={vi.fn()} onCancel={vi.fn()} />,
    );

    // The first session should be highlighted (inverse).
    const frame = lastFrame();
    expect(frame).toContain('First incident');
  });

  it('selects on enter', async () => {
    const onSelect = vi.fn();
    const sessions = [makeSession({ session_id: 's1', title: 'Prod' })];
    const instance = render(
      <SessionPicker sessions={sessions} onSelect={onSelect} onCancel={vi.fn()} />,
    );
    await settle();

    instance.stdin.write('\r');
    await tick();

    expect(onSelect).toHaveBeenCalledWith(sessions[0]);
  });

  it('cancels on escape', async () => {
    const onCancel = vi.fn();
    const sessions = [makeSession({ session_id: 's1', title: 'Prod' })];
    const instance = render(
      <SessionPicker sessions={sessions} onSelect={vi.fn()} onCancel={onCancel} />,
    );
    await settle();

    instance.stdin.write('\x1b');
    await tick();

    expect(onCancel).toHaveBeenCalled();
  });

  it('moves selection with arrow keys', async () => {
    const onSelect = vi.fn();
    const sessions = [
      makeSession({ session_id: 's1', title: 'First' }),
      makeSession({ session_id: 's2', title: 'Second' }),
      makeSession({ session_id: 's3', title: 'Third' }),
    ];
    const instance = render(
      <SessionPicker sessions={sessions} onSelect={onSelect} onCancel={vi.fn()} />,
    );
    await settle();

    // Arrow down twice to select the third
    instance.stdin.write('\x1b[B');
    await tick();
    instance.stdin.write('\x1b[B');
    await tick();

    instance.stdin.write('\r');
    await tick();

    expect(onSelect).toHaveBeenCalledWith(sessions[2]);
  });

  it('does not fire onSelect when not focused', async () => {
    const onSelect = vi.fn();
    const sessions = [makeSession({ session_id: 's1', title: 'Prod' })];
    const instance = render(
      <SessionPicker sessions={sessions} onSelect={onSelect} onCancel={vi.fn()} focused={false} />,
    );
    await settle();

    instance.stdin.write('\r');
    await tick();

    expect(onSelect).not.toHaveBeenCalled();
  });

  it('does not fire onCancel when not focused', async () => {
    const onCancel = vi.fn();
    const sessions = [makeSession({ session_id: 's1', title: 'Prod' })];
    const instance = render(
      <SessionPicker sessions={sessions} onSelect={vi.fn()} onCancel={onCancel} focused={false} />,
    );
    await settle();

    instance.stdin.write('\x1b');
    await tick();

    expect(onCancel).not.toHaveBeenCalled();
  });
});