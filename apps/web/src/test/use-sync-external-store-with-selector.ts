/**
 * Local `useSyncExternalStoreWithSelector` implementation.
 *
 * The npm `use-sync-external-store` shim is hoisted to the repository root in
 * this monorepo (shared by `@tanstack/react-router`'s `react-store`), where it
 * binds to the CLI workspace's React 18. The web workspace runs React 19, so
 * mixing the two hook systems crashes with "Cannot read properties of null".
 *
 * This module mirrors React's MIT-licensed shim (`with-selector`) but imports
 * React from this workspace, so Vite's `react` alias keeps every consumer on
 * the single React 19 copy. It is used only by tests via a resolve alias for
 * `use-sync-external-store/shim/with-selector`.
 */
import { useDebugValue, useRef, useSyncExternalStore } from 'react';

function defaultIsEqual(a: unknown, b: unknown): boolean {
  return a === b;
}

export function useSyncExternalStoreWithSelector<Snapshot, Selection>(
  subscribe: (onStoreChange: () => void) => () => void,
  getSnapshot: () => Snapshot,
  getServerSnapshot: () => Snapshot,
  selector: (snapshot: Snapshot) => Selection,
  isEqual?: (a: Selection, b: Selection) => boolean,
): Selection {
  const instRef = useRef<{ hasValue: boolean; value: Selection } | null>(null);
  let inst = instRef.current;
  if (inst === null) {
    inst = { hasValue: false, value: undefined as unknown as Selection };
    instRef.current = inst;
  }

  const getSelection = (): Selection => {
    const selected = selector(getSnapshot());
    const equal = isEqual ?? defaultIsEqual;
    if (inst.hasValue && equal(inst.value, selected)) {
      return inst.value;
    }
    inst.hasValue = true;
    inst.value = selected;
    return selected;
  };

  const serverSelection = () => selector(getServerSnapshot());

  const value = useSyncExternalStore(subscribe, getSelection, serverSelection);
  useDebugValue(value);
  return value;
}
