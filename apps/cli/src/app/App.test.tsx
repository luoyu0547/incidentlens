import { describe, expect, it } from 'vitest';
import { render } from 'ink-testing-library';
import React from 'react';
import { Text } from 'ink';
import { Conversation } from '../ui/Conversation.js';
import type { ApprovalBlock } from '../state/cli-state.js';

describe('Ink rendering', () => {
  it('renders simple text', () => {
    const { lastFrame } = render(<Text>Hello World</Text>);

    expect(lastFrame()).toContain('Hello World');
  });

  it('renders colored text', () => {
    const { lastFrame } = render(<Text color="blue">Blue Text</Text>);

    expect(lastFrame()).toContain('Blue Text');
  });

  it('exposes approval actions before any decision is persisted', () => {
    const approval: ApprovalBlock = { kind: 'approval', approvalId: 'apr-1', status: 'pending' };
    const { lastFrame } = render(<Conversation messages={[approval]} />);

    expect(lastFrame()).toMatch(/需要审批/);
    expect(lastFrame()).toMatch(/\[A\].*批准.*\[R\].*拒绝.*\[D\].*差异/);
    expect(lastFrame()).not.toMatch(/已批准|approved/);
  });
});
