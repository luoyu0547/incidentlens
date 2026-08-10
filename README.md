# IncidentLens

IncidentLens is being rebuilt as a safety-first control plane for diagnosing
services across registered cloud servers. It is not a local fault-injection
demo and it does not expose a general shell to an AI model.

## Phase 1: Local Runtime and Project Registry

The current implementation provides a **local-only runtime** that does not yet
open SSH sessions to remote servers. It manages project metadata, events, and
local Docker Compose configurations.

### Features

- **Local runtime lifecycle** with SQLite persistence
- **Project registry** for managing Docker Compose projects
- **Durable runtime events** with live fan-out
- **HTTP/WebSocket API** for project management and event streaming

### Quick Start

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest -q

# Run code quality checks
uv run ruff check .

# Start the API server
uv run uvicorn incidentlens_control_plane.main:app --reload
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `INCIDENTLENS_DATA_DIR` | Data directory for SQLite database | `~/.incidentlens` |

To use a custom data directory:

```bash
export INCIDENTLENS_DATA_DIR="/path/to/your/data"
uv run uvicorn incidentlens_control_plane.main:app --reload
```

### API Endpoints

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

### Quick API Test

```bash
# Health check
curl http://127.0.0.1:8765/healthz

# Create a project
curl -X POST http://127.0.0.1:8765/api/projects \
  -H 'content-type: application/json' \
  -d '{"project_id":"demo","display_name":"Demo","local_source_paths":[],"targets":[],"services":[]}'

# List projects
curl http://127.0.0.1:8765/api/projects

# List events
curl 'http://127.0.0.1:8765/api/events?after=0'
```

For detailed verification instructions, see [Phase 1 Verification Guide](docs/phase-1-local-runtime-verification.md).

## Development

Run the full test suite:

```bash
uv run pytest -q
uv run ruff check .
```

### Code Structure

- `apps/control-plane/src/incidentlens_control_plane/` - Main application
  - `main.py` - Application entry point
  - `runtime.py` - Runtime lifecycle management
  - `routes.py` - HTTP/WebSocket endpoints
  - `registry.py` - Project registry
  - `events.py` - Event store and fan-out
  - `state.py` - Application state
  - `types.py` - Pydantic models

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
