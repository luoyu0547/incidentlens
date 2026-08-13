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

## Phase 4: Bounded Agent Runtime

Phase 4 adds a bounded investigation agent runtime over the same registered
targets. It is provider-neutral: the orchestrator drives a `ModelProvider`
contract, and the only provider in the repository is a deterministic scripted
`FakeProvider` — no real model is ever contacted. Every agent-visible external
fact comes from the append-only, redacted evidence store, and every remote call
stays behind the Phase 2/3 policy, approval, session and gateway gates.

### Features

- **Provider-neutral model contract** — a provider may only propose tool calls,
  child delegations, hypotheses, conclusions and a stop signal; it never
  executes tools, writes stores or sees raw transcripts
  (`ProviderOutputValidator` rejects un-allowlisted, out-of-scope or
  ungrounded output)
- **Bounded parent / container-child loops** — per-run and per-investigation
  budgets on rounds, tool calls, wall-clock, output bytes, evidence and
  no-new-evidence rounds; a parent can concurrently delegate independent
  container-scoped children, each with its own scope/session/evidence package
- **Checkpoints, cancel, resume and restart recovery** — runs resume from their
  latest checkpoint; startup recovery marks a dangerous in-flight call
  `UNCERTAIN` and never replays it, while a safe read-only call is repaired and
  resumable; shutdown is ordered investigations → subscriptions → sessions
- **Evidence-grounded structured output** — hypotheses, conclusions and child
  reports may only cite evidence the run actually owns; empty citations are a
  missing-evidence pause, never a fabrication
- **Approval pause/resume** — shell/PTTY, file mutations and docker actions that
  policy classifies as approval-required park the run `WAITING_APPROVAL`;
  approving re-executes the exact single-use intent once
- **Source discovery and registry proposals** — discovery stays inside
  registered bounds; unregistered containers/paths are surfaced as candidates
  with the evidence that exposed them, and only an approved proposal widens the
  registry after re-validation
- **Investigation REST API and durable/live events** — `/api/investigations`,
  `/api/evidence` and shared `/api/events`; payloads carry IDs, statuses and
  bounded redacted summaries only

### Quick Start (live verification)

The live agent acceptance test is opt-in and skipped by default:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/investigation tests/evidence tests/remote_ops tests/logs tests/events tests/web tests/test_app.py -q
INCIDENTLENS_RUN_LIVE_AGENT_TESTS=1 UV_CACHE_DIR=.uv-cache uv run pytest tests/integration/test_live_agent_runtime.py -q
```

The fixture skips with a clear reason when the variable is absent or Docker is
unavailable. See [Phase 4 Verification Guide](docs/phase-4-agent-runtime-verification.md)
for the full offline and opt-in live command set, including how the parent
delegates container children, approval pause/resume, restart checkpoints and
the uncertain no-replay recovery path.

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
  - `main.py` - FastAPI entry point and lifespan (startup recovery, shutdown order)
  - `runtime.py` - `RuntimeServices` container and `build_runtime()` assembly
  - `config.py` - Bounded `RuntimeSettings` (log subscription + agent budget caps)
  - `project_registry/` - Project/target/service/path registry store and types
  - `remote_ops/` - AsyncSSH transport, `SessionManager`, `RemoteToolGateway`, command policy, persistent shell, session lifecycle
  - `logs/` - Log parsing, redaction, signals, correlation, store (FTS5), service, subscriptions
  - `evidence/` - Typed append-only redacted evidence store and service
  - `approvals/` - Exact single-use approval service and store
  - `changes/` - Two-location backup ChangeSet manager and encrypted vault
  - `events/` - Durable runtime event store and live broker
  - `investigation/` - Phase 4 bounded agent runtime: provider contract, Fake Provider, tool registry/executor, orchestrator, service, source discovery, registry proposals, recovery, events
  - `routes/` - HTTP/WebSocket endpoints (projects, remote-sessions, logs, evidence, investigations, approvals, changes, events)

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
