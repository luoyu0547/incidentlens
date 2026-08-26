# Task 12 report

Implemented the workspace SSE invalidation protocol and App Shell bridge.

- Added Zod-validated resource.changed and stream.gap schemas and EventSource connection lifecycle in `packages/protocol/src/workspace-events.ts`.
- Exported the protocol API from `packages/protocol/src/index.ts`.
- Added a single root `WorkspaceEventBridge` with precise resource invalidation, full invalidation on gaps, status display, and idempotent cleanup.
- Added protocol and web smoke tests.
- Applied the minimal query-key typing fix: `canonicalFilter` accepts `object` and casts only for key access.

Verification: targeted test commands could not run before dependencies/test files were available; protocol test was added. Web typecheck remains blocked by missing installed dependencies (`@tanstack/react-router`, `@testing-library/react`) in the worktree. Existing query key errors were fixed.

Concerns: EventSource exposes authentication failures only through browser error behavior; the implementation bounds reconnect attempts and closes after the retry budget, but cannot inspect HTTP status in standard browser EventSource.
