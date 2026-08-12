# IncidentLens

IncidentLens is being rebuilt as a safety-first control plane for diagnosing
services across registered cloud servers. It is not a local fault-injection
demo and it does not expose a general shell to an AI model. **No server-side
IncidentLens agent is installed on remote hosts.**

## Phase 2: Persistent Safe Remote Tools

Phase 2 adds a safety boundary for real remote infrastructure. It never gives
the model a general-purpose shell or SSH tool; instead it exposes typed remote
operations behind policy, approval, and backup gates.

### Features

- **One persistent SSH connection** per registered target, with SFTP, command,
  and shell channels (`SessionManager` + `AsyncSshTransport`)
- **Persistent shell state** — `cd` and environment survive across commands
- **Scoped read/list/search/stat** file tools bounded to allowed roots
- **Large multi-location Edit/Write** with mandatory two-backup ordering:
  an encrypted local backup plus a timestamped same-directory remote backup
- **Stale-write detection and atomic replace** with multi-file rollback
- **Permanent `rm -rf` denial** and a three-tier command policy
  (automatic / approval-required / forbidden)
- **Exact, single-use approvals** for Docker/service-impacting operations
- **Durable audit events** and encrypted local backup vault

### Quick Start (live verification)

The live acceptance test is opt-in and requires Docker plus `ssh-keygen`. It
starts a disposable OpenSSH container (`infra/test-ssh`), exercises all eight
acceptance points, and tears the container down:

```bash
uv run pytest -q                                          # unit tests
INCIDENTLENS_RUN_LIVE_SSH=1 uv run pytest tests/integration/test_live_ssh_tools.py -q
```

The fixture skips with a clear reason when the variable is absent or Docker is
unavailable. See [Phase 2 Verification Guide](docs/phase-2-remote-tools-verification.md)
for registration JSON, manual connection/read/edit examples, backup locations,
the approval flow, rollback, and cleanup.

## Phase 3: Hybrid Log Evidence

Phase 3 adds a log investigation pipeline over the same registered targets. It
never stores or returns raw log text: every line is parsed, redacted
(secrets/tokens/emails/IPs), truncated at 16 KiB, and only the redacted message
is persisted, streamed, or cited as evidence.

### Features

- **On-demand log queries** for host files and registered containers
  (`POST /api/logs/query`) with conservative redaction
- **Full-text search** over persisted redacted records
  (`GET /api/logs/search`)
- **Opt-in persistent subscriptions** with pause/resume/delete, stored cursors,
  restart recovery, and WebSocket replay/live dedupe
  (`/api/logs/subscriptions`, `WS /api/logs/subscriptions/{id}/ws`)
- **Append-only evidence** built exclusively from redacted content
  (`POST /api/evidence/from-log-records`)
- **Runtime lifecycle ordering** — active subscriptions are restored at startup
  before requests and closed before SSH sessions on shutdown

### Quick Start (live verification)

The live log acceptance test is opt-in and skipped by default:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/logs tests/evidence tests/remote_ops tests/web tests/events tests/test_app.py -q
INCIDENTLENS_RUN_LIVE_LOG_TESTS=1 uv run pytest tests/integration/test_live_log_tools.py -q
```

The fixture skips with a clear reason when the variable is absent or Docker is
unavailable. See [Phase 3 Verification Guide](docs/phase-3-hybrid-log-evidence-verification.md)
for the full offline and opt-in live command set.

## Phase 1: Local Runtime and Project Registry

Phase 1 provides a **local-only runtime** and the **project registry** that
Phase 2's remote tools build on. It manages project metadata, events, and
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
