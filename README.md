# IncidentLens

IncidentLens is being rebuilt as a safety-first control plane for diagnosing
services across registered cloud servers. It is not a local fault-injection
demo and it does not expose a general shell to an AI model.

## Phase 1: Local Runtime and Project Registry

The current implementation provides:
- Local runtime lifecycle with SQLite persistence
- Project registry for managing Docker Compose projects
- Durable runtime events with live fan-out
- HTTP/WebSocket API for project management and event streaming

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

### API Endpoints

- `GET /healthz` - Health check
- `POST /api/projects` - Create project
- `GET /api/projects` - List projects
- `GET /api/projects/{project_id}` - Get project
- `PUT /api/projects/{project_id}` - Update project
- `DELETE /api/projects/{project_id}` - Delete project
- `GET /api/events?after=<sequence>` - List events
- `WS /api/events/ws?after=<sequence>` - WebSocket event stream

### Environment Variables

- `INCIDENTLENS_DATA_DIR` - Data directory (default: `~/.incidentlens`)

## Development

Run the full test suite:

```bash
uv run pytest -q
uv run ruff check .
```
