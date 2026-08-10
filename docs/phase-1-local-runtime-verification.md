# Phase 1: Local Runtime Verification

This document verifies that Phase 1 (Local Runtime and Project Registry) is complete and functional.

## Verification Steps

### 1. Start the API Server

```bash
export INCIDENTLENS_DATA_DIR="$(mktemp -d)/incidentlens"
uv run uvicorn incidentlens_control_plane.main:app --port 8765
```

### 2. Test Health Check

```bash
curl -sS http://127.0.0.1:8765/healthz
```

Expected response:
```json
{"status": "ok", "remote_execution": "not_configured"}
```

### 3. Test Project CRUD

**Create a project:**
```bash
curl -sS -X POST http://127.0.0.1:8765/api/projects \
  -H 'content-type: application/json' \
  -d '{
    "project_id": "demo",
    "display_name": "Demo Project",
    "local_source_paths": [],
    "targets": [],
    "services": []
  }'
```

Expected: HTTP 201 Created

**List projects:**
```bash
curl -sS http://127.0.0.1:8765/api/projects
```

Expected: Returns the created project

**Get project:**
```bash
curl -sS http://127.0.0.1:8765/api/projects/demo
```

Expected: Returns the project details

### 4. Test Event Streaming

**List events:**
```bash
curl -sS 'http://127.0.0.1:8765/api/events?after=0'
```

Expected: Contains `project.created` event

**WebSocket test:**
```bash
# Using websocat or similar tool
websocat ws://127.0.0.1:8765/api/events/ws?after=0
```

### 5. Test Persistence

Restart the server with the same `INCIDENTLENS_DATA_DIR` and verify:
- Projects persist across restarts
- Events persist across restarts

## Test Suite

Run the full test suite:
```bash
uv run pytest -q
```

Expected: All tests pass

## Code Quality

Run code quality checks:
```bash
uv run ruff check .
```

Expected: No issues found

## Phase Completion Criteria

- [x] Local runtime lifecycle with SQLite persistence
- [x] Project registry for managing Docker Compose projects
- [x] Durable runtime events with live fan-out
- [x] HTTP/WebSocket API for project management and event streaming
- [x] All tests pass
- [x] Code quality checks pass
- [x] Documentation updated

## Next Phase

Phase 2 will add:
- Persistent SSH sessions
- Remote file operations
- Safe change management with backups and approval