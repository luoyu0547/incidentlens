import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { App } from '../src/App';

describe('App Shell', () => {
  it('identifies the read-only observability workspace', () => {
    render(<App />);
    expect(screen.getByRole('heading', { name: 'IncidentLens' })).toBeVisible();
    expect(screen.getByRole('navigation')).toHaveTextContent('总览');
    expect(
      screen.queryByRole('button', { name: /approve|reject|execute|restart|rollback/i }),
    ).toBeNull();
  });
});
