import { describe, expect, it, vi } from 'vitest';

// The project lockfile currently pairs Ink with a React reconciler that cannot
// initialize under React 19. Mocking only Ink lets this focused unit test load
// the card's security gate without changing production dependencies.
vi.mock('ink', () => ({
  Box: 'box',
  Text: 'text',
  useInput: vi.fn(),
}));

import { approvalHotkeysActive } from './ApprovalCard.js';

describe('ApprovalCard safety gates', () => {
  it('enables A/R/D only for a focused card with an empty prompt and no overlay', () => {
    expect(approvalHotkeysActive(true, true, false)).toBe(true);
    expect(approvalHotkeysActive(false, true, false)).toBe(false);
    expect(approvalHotkeysActive(true, false, false)).toBe(false);
    expect(approvalHotkeysActive(true, true, true)).toBe(false);
  });
});
