# Phase 3 Hybrid Log Evidence Verification

Phase 3 adds the log investigation pipeline: on-demand file/docker log queries,
conservative secret redaction with 16 KiB truncation, a SQLite-backed store
(`log_records`, FTS5, `log_subscriptions`, `log_cursors`, `log_subscription_runs`),
opt-in persistent subscriptions, append-only evidence built exclusively from
redacted content, WebSocket replay/live dedupe, and runtime lifecycle ordering
(subscriptions are restored at startup before requests, and closed before SSH
sessions on shutdown).

The lifecycle ordering regression test drives the real FastAPI lifespan:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/web/test_log_subscriptions_api.py::test_lifespan_restores_subscriptions_before_requests_and_closes_before_sessions -q
```

## Default offline checks

These run on every change and never touch a network.  The opt-in live test is
skipped by default.

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/logs tests/evidence tests/remote_ops tests/web tests/events tests/test_app.py -q
UV_CACHE_DIR=.uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane tests
```

## Opt-in live checks

The live acceptance test is DISABLED by default.  It starts the disposable
`infra/test-ssh` OpenSSH container, exercises host-file query redaction,
opt-in subscription streaming, restart-resume with no duplicate at the replay
boundary, redacted-only evidence, and (when a docker CLI is available on the
target) container list/search plus docker log queries.  Set
`INCIDENTLENS_RUN_LIVE_LOG_TESTS=1` to opt in:

```bash
INCIDENTLENS_RUN_LIVE_LOG_TESTS=1 UV_CACHE_DIR=.uv-cache uv run pytest tests/integration/test_live_log_tools.py -q
```

Live checks require the existing test SSH/Docker environment and never run by
default.
