# Task 1 Report: Establish npm Workspace and Executable CLI Package

## Status

**Completed** - Commit `c131cf7` on `feat/hard-cloud-incident`

## Changes

### New Files

| File | Purpose |
|------|---------|
| `package.json` | Root workspace with `apps/*` and `packages/*` roots |
| `package-lock.json` | Locked dependency versions |
| `.nvmrc` | Pin Node.js 22.19.0 |
| `tsconfig.base.json` | Shared TypeScript config (ES2023, NodeNext, strict) |
| `eslint.config.mjs` | ESLint 9 flat config with typescript-eslint and react-hooks |
| `prettier.config.mjs` | Prettier formatting rules |
| `apps/cli/package.json` | `@incidentlens/cli` with ink, react, ws, zod, tsup, vitest |
| `apps/cli/tsconfig.json` | CLI-specific TypeScript config extending base |
| `apps/cli/tsup.config.ts` | ESM build with shebang and source maps |
| `apps/cli/vitest.config.ts` | Vitest test runner config |
| `apps/cli/src/cli.tsx` | Minimal CLI entry with --version flag and Ink component |
| `apps/cli/test/package-metadata.test.ts` | Validates package name, bin, engines, type, shebang |

### Modified Files

| File | Change |
|------|--------|
| `.gitignore` | Added `node_modules/`, `dist/`, `*.tsbuildinfo` |

## Verification

```
$ npm run lint
> eslint .
(empty = pass)

$ npm run format:check
> prettier --check .
All matched files use Prettier code style!

$ npm run typecheck
> tsc --noEmit
(empty = pass)

$ npm test --workspace @incidentlens/cli -- package-metadata.test.ts
✓ test/package-metadata.test.ts (2 tests) 6ms
Tests  2 passed (2)

$ npm run build --workspace @incidentlens/cli
ESM Build success in 14ms

$ node apps/cli/dist/cli.js --version
0.1.0
```

## Concerns

1. **Prettier reformatted 61 existing files** - The initial `npm run format:check` failed because pre-existing docs/infra files were not formatted. Running `npx prettier --write .` fixed them, but these changes are intentionally **not** in the Task 1 commit to keep scope clean. They should be committed separately or included in a repo-wide format pass.

2. **npm audit reports 5 vulnerabilities** - The `npm install` output shows 5 vulnerabilities (3 moderate, 1 high, 1 critical). These appear to be in transitive dependencies. Should be addressed in a follow-up.

3. **Root ESLint config is global** - The `eslint.config.mjs` at root applies to all workspaces. The `.venv/` exclusion was needed to prevent Python virtualenv JS files from being linted. Future workspaces (protocol, web) may need additional ignore patterns.

4. **Node version pinning** - `.nvmrc` specifies 22.19.0. The `engines` field in root `package.json` uses `>=22.19.0`. Ensure CI uses this version.
