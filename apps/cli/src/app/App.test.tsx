import { describe, expect, it } from 'vitest';
import { render } from 'ink-testing-library';
import React from 'react';
import { Text } from 'ink';

describe('Ink rendering', () => {
  it('renders simple text', () => {
    const { lastFrame } = render(<Text>Hello World</Text>);

    expect(lastFrame()).toContain('Hello World');
  });

  it('renders colored text', () => {
    const { lastFrame } = render(<Text color="blue">Blue Text</Text>);

    expect(lastFrame()).toContain('Blue Text');
  });

  it('renders bold text', () => {
    const { lastFrame } = render(<Text bold>Bold Text</Text>);

    expect(lastFrame()).toContain('Bold Text');
  });
});
