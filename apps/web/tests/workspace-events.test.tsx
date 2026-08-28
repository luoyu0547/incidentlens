import { describe, expect, it } from 'vitest';
import { WorkspaceEventBridge } from '../src/app/WorkspaceEventBridge';

describe('WorkspaceEventBridge', () => {
  it('exports a root bridge component', () => expect(WorkspaceEventBridge).toBeTypeOf('function'));
});
