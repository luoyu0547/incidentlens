# Task 15 Report

## Delivered

- Merged `feat/hard-cloud-incident` into this task worktree before packaging work.
- Prepared `@incidentlens/cli` for public npm distribution: public access metadata, Node `>=22.19.0`, restricted package files, optional native keyring, bundled CLI, and `prepack` validation.
- Added package README/license, install smoke coverage, CLI CI matrix, and tag-gated provenance release workflow. No package was published.
- Tightened the Node runtime guard so Node 22.0–22.18 is rejected with the supported-version message.

## Verification

- `npm ci`: passed.
- CLI typecheck: passed after aligning Ink/React package versions.
- `npm test --workspace @incidentlens/cli -- test/package/install-smoke.test.ts`: passed; the tarball contents are restricted and an isolated install runs `incidentlens --version`.
- `uv run pytest -q` and `uv run ruff check .`: not runnable because `uv` is absent from this environment.

## Remaining concern

The full CLI suite / `prepack` still has unrelated existing failures in `src/stream/ws-event-stream.test.ts` and Ink component tests; these must be resolved before release packaging can pass without `--ignore-scripts`.
