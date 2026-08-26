# Task 15 report

Implemented FastAPI serving for the Vite workspace.

- Added `mount_web_assets()` with an allow-listed SPA fallback for `/`, `/services/*`, `/issues*`, and `/investigations/*`.
- Preserved API, WebSocket, health, and missing-asset 404 behavior; filename-like and reserved paths never fall back to HTML.
- Served `/assets/*` with immutable caching and the SPA index with `no-cache`.
- Missing build output returns an explicit 503 for the SPA while allowing the application to start.
- Configured Vite to emit the manifest and production assets into the Python package static directory.
- Added runtime `web_root` configuration and excluded generated static output from source control.

Verification:

- `npm run web:build` passed and generated `index.html`, manifest, and hashed JS/CSS assets.
- `uv run pytest tests/web/test_spa_assets.py tests/test_app.py -q` could not run because `uv` is unavailable.
- `python3 -m pytest tests/web/test_spa_assets.py tests/test_app.py -q` could not run because pytest is unavailable.
- `uv build` could not run because `uv` is unavailable.

Commit: `f594b28 feat(web): serve Vite workspace from FastAPI`
