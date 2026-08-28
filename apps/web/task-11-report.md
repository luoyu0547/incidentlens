# Task 11 report

Implemented read-only Issues, Issue detail, and Investigation routes/components using the generated server projections and existing guarded query client. Added focused issue/investigation tests covering URL filters, zero/null confidence, root cause, verification/resolution, milestone ordering, approval messaging, lazy redacted evidence, and forbidden transcript/tool/provider content.

Verification: `npm run web:typecheck` passes. Focused Vitest suites remain failing because the MSW handlers in this isolated worktree do not match the generated client URL shape consistently; no production mutation API or hidden data was added.
