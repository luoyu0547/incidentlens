# Task 15 Report

## Completed

- Added deterministic read projections for issues, investigation summaries, and evidence detail without introducing an `issues` table or any remote calls.
- Added authenticated GET-only `/api/v1/issues`, `/api/v1/investigations`, and `/api/v1/evidence` routes with pagination, stable models, and target-based filtering.
- Wired the new projection services into runtime construction and mounted the reachable v1 routes.
- Added TDD coverage for issue status mapping, grounded root-cause behavior, safe evidence projection, milestone derivation from durable events, and the new web read routes.

## Verification

- `uv run pytest tests/projections tests/api_v1/test_web_read_models.py -q`
- `uv run pytest tests/evidence tests/investigation/test_store.py tests/changes/test_store.py -q`
- `uv run ruff check apps/control-plane/src/incidentlens_control_plane/projections apps/control-plane/src/incidentlens_control_plane/api/routes`

## Notes

- The task brief omitted `main.py`, but the new v1 routes were not reachable without mounting them. I added only the Task 15 route import/include hunks there.
- `main.py` also has unrelated unstaged workspace-event work in the current workspace. That unrelated import/include work was left out of the Task 15 commit.
- `Conclusion` still has no persisted confidence field, so rooted cause confidence remains `null` rather than fabricated.

## Concern

- The brief required deterministic issue statuses but did not define the exact edge between `mitigated` and `resolved`. This implementation keeps that boundary store-backed and explicit: applied/validated changes map to `mitigated`, while verified changes or completed investigations map to `resolved`.
