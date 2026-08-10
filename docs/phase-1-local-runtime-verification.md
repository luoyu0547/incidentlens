# Phase 1: Local Runtime Verification

This document verifies that Phase 1 (Local Runtime and Project Registry) is complete and functional. It provides exact commands, expected responses, and instructions for testing data persistence across restarts.

## Prerequisites

Ensure dependencies are installed:

```bash
uv sync
```

## Verification Steps

### 1. Start the API Server

Start the Uvicorn server with a temporary data directory to ensure clean state:

```bash
export INCIDENTLENS_DATA_DIR="$(mktemp -d)/incidentlens"
uv run uvicorn incidentlens_control_plane.main:app --port 8765
```

### 2. Run Verification Commands (Second Terminal)

Open a second terminal and run the following commands in order:

#### Health Check

```bash
curl -sS http://127.0.0.1:8765/healthz
```

**Expected Response:**
```json
{
  "status": "ok",
  "remote_execution": "unconfigured"
}
```

The `remote_execution: unconfigured` confirms this is a local-only runtime without SSH sessions.

#### Create a Project

```bash
curl -sS -X POST http://127.0.0.1:8765/api/projects \
  -H 'content-type: application/json' \
  -d '{"project_id":"demo","display_name":"Demo","local_source_paths":[],"targets":[],"services":[]}'
```

**Expected Response:**
```json
{
  "project_id": "demo",
  "display_name": "Demo",
  "local_source_paths": [],
  "targets": [],
  "services": [],
  "created_at": "2026-08-10T...",
  "updated_at": "2026-08-10T..."
}
```

**Expected HTTP Status:** `201 Created`

#### List Projects

```bash
curl -sS http://127.0.0.1:8765/api/projects
```

**Expected Response:**
```json
[
  {
    "project_id": "demo",
    "display_name": "Demo",
    "local_source_paths": [],
    "targets": [],
    "services": [],
    "created_at": "2026-08-10T...",
    "updated_at": "2026-08-10T..."
  }
]
```

The `demo` project should appear in the list.

#### Get Events

```bash
curl -sS 'http://127.0.0.1:8765/api/events?after=0'
```

**Expected Response:**
```json
{
  "events": [
    {
      "sequence": 1,
      "event_type": "project.created",
      "project_id": "demo",
      "timestamp": "2026-08-10T...",
      "payload": {
        "project_id": "demo",
        "display_name": "Demo"
      }
    }
  ],
  "next_sequence": 2
}
```

The events stream should contain a `project.created` event for the demo project.

### 3. Test Data Persistence Across Restart

This section verifies that data persists in SQLite across server restarts.

#### Step 1: Note the Project ID

The project ID is `demo`.

#### Step 2: Stop the Server

Press `Ctrl+C` in the terminal running Uvicorn.

#### Step 3: Restart with Same Data Directory

**Important:** Use the same `INCIDENTLENS_DATA_DIR` value. The project data persists in SQLite at `$INCIDENTLENS_DATA_DIR/incidentlens.db`.

```bash
# Use the same INCIDENTLENS_DATA_DIR from Step 1
uv run uvicorn incidentlens_control_plane.main:app --port 8765
```

#### Step 4: Verify Persistence

```bash
curl -sS http://127.0.0.1:8765/api/projects
```

**Expected:** The `demo` project should still appear in the list.

```bash
curl -sS 'http://127.0.0.1:8765/api/events?after=0'
```

**Expected:** The events from the previous session should still be present.

### 4. Additional Endpoint Tests

#### Get Project by ID

```bash
curl -sS http://127.0.0.1:8765/api/projects/demo
```

**Expected Response:** The full project object with `project_id: "demo"`.

#### Update Project

```bash
curl -sS -X PUT http://127.0.0.1:8765/api/projects/demo \
  -H 'content-type: application/json' \
  -d '{"display_name":"Updated Demo","local_source_paths":["/tmp"],"targets":[],"services":[]}'
```

**Expected Response:** The updated project object with `display_name: "Updated Demo"`.

#### Delete Project

```bash
curl -sS -X DELETE http://127.0.0.1:8765/api/projects/demo
```

**Expected HTTP Status:** `204 No Content`

#### Verify Deletion

```bash
curl -sS http://127.0.0.1:8765/api/projects
```

**Expected Response:** `[]` (empty array)

## Automated Verification

Run the automated test suite to verify all endpoints:

```bash
uv run pytest -q
```

**Expected:** All tests pass (exit code 0).

Run code quality checks:

```bash
uv run ruff check .
```

**Expected:** No linting errors (exit code 0).

## API Endpoints Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/healthz` | Health check with remote execution status |
| POST | `/api/projects` | Create a new project |
| GET | `/api/projects` | List all projects |
| GET | `/api/projects/{project_id}` | Get a specific project |
| PUT | `/api/projects/{project_id}` | Update a project |
| DELETE | `/api/projects/{project_id}` | Delete a project |
| GET | `/api/events?after=<sequence>` | List events after a sequence number |
| WS | `/api/events/ws?after=<sequence>` | WebSocket event stream |

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `INCIDENTLENS_DATA_DIR` | Data directory for SQLite database | `~/.incidentlens` |

## Phase 1 Scope

This phase implements:
- Local runtime lifecycle with SQLite persistence
- Project registry for managing Docker Compose projects
- Durable runtime events with live fan-out
- HTTP/WebSocket API for project management and event streaming

This phase does NOT implement:
- SSH sessions to remote servers
- Docker container execution
- Model provider integration
- Web frontend

## Troubleshooting

**Issue:** Server fails to start
- Ensure `uv sync` has been run
- Check that port 8765 is not in use

**Issue:** Data does not persist
- Verify `INCIDENTLENS_DATA_DIR` is set correctly
- Check that the directory exists and is writable

**Issue:** Tests fail
- Run `uv sync` to ensure dependencies are installed
- Check Python version compatibility
