# Task 14 Report

## Completed

- Added new read-model contracts under `incidentlens_control_plane/projections`.
- Implemented on-demand overview and service projections without adding tables or remote actions.
- Added authenticated `GET /api/v1/overview` and `GET /api/v1/services/{service_id}` routes.
- Wired the new projection services into runtime construction.
- Mounted the new routes in `main.py`.

## Verification

- `uv run pytest tests/projections/test_overview.py tests/projections/test_services.py tests/api_v1/test_overview_services.py -q`
- `uv run ruff check apps/control-plane/src/incidentlens_control_plane/projections apps/control-plane/src/incidentlens_control_plane/api/routes/overview.py apps/control-plane/src/incidentlens_control_plane/api/routes/services.py`

## Notes

- The task brief's file list omitted `main.py`, but the routes were not reachable without mounting them. I added only the overview/service route import and `include_router(...)` hunks there.
- The workspace already had unrelated unstaged SSE work in `main.py`, plus new `workspace_events.py` and `streams/workspace.py`. I did not reset or stage that unrelated work.

## Concern

- A broader contract check (`uv run pytest tests/contracts/test_product_contracts.py -q`) is still red in the current workspace because checked-in product contract / stream export artifacts are already out of sync with the live app state, including unrelated workspace stream work. I left that out of this commit to avoid mixing Task 14 with the existing SSE/contract drift.

## Review Fixes

- Removed approval IDs from the service read response and replaced them with safe pending-approval counts, so the projection no longer exposes approval handles on the web read surface.
- Tightened approval correlation to exact facade target IDs or linked investigation IDs only; raw registry target IDs are no longer used for matching.
- Replaced the capped ascending log scan with direct error-window / latest-observation queries so recent errors and timestamps are found even when older INFO history exceeds 1000 rows.
- Updated health semantics so failed investigations degrade service health, and mixed `(healthy, unknown)` aggregates now resolve to `unknown` instead of `healthy`.
- Added focused regressions for approval-target collisions, deep old-log history with a recent error, failed-investigation degradation, and mixed healthy/unknown aggregation.
