# IncidentLens

Evidence-driven microservice incident investigation agent.

## Overview

IncidentLens automates root-cause analysis for microservice incidents by:
1. Receiving alerts with error signals
2. Calling read-only diagnostic tools (search logs, query metrics, etc.)
3. Recording evidence and updating hypotheses
4. Verifying root causes with confidence thresholds
5. Generating structured investigation reports

## Architecture

```
+-------------+     +--------------+     +----------------+
|   Gateway   |---->|    Order     |---->|    Payment     |
|  Service    |     |   Service    |     |    Service     |
+-------------+     +--------------+     +----------------+
       |                    |                     |
       +--------------------+---------------------+
                            |
                    +-------v-------+
                    |  Control Plane |
                    |  (Investigation|
                    |   Engine)      |
                    +---------------+
```

## Quick Start

### Local Development

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest -q

# Start control plane
uv run uvicorn incidentlens_control_plane.main:app --port 8003
```

### Docker Compose

```bash
# Start all services
docker compose -f infra/compose/compose.yaml up --build

# Generate traffic
uv run python scripts/generate_traffic.py --url http://localhost:8000

# Open dashboard
open http://localhost:8003
```

### Verified End-to-End Workflow

```bash
# 1. Start the full stack
docker compose -f infra/compose/compose.yaml up --build

# 2. Run all demo scenarios via the CLI
uv run python scripts/run_demo.py --all --compose

# 3. Run the five-scenario Compose acceptance tests
uv run pytest tests/integration/test_scenario_acceptance.py -q

# 4. Tear down when done
docker compose -f infra/compose/compose.yaml down -v
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| Gateway | 8000 | Proxies requests to order service |
| Order | 8001 | Creates orders, calls payment service |
| Payment | 8002 | Processes charge requests |
| Control Plane | 8003 | Investigation engine, tools, SSE, dashboard |

## API Endpoints

### Telemetry
- `POST /api/telemetry/events` — Receive and persist telemetry events

### Investigations
- `POST /api/investigations/start` — Start a new investigation
- `POST /api/investigations/{incident_id}/round` — Run one investigation round
- `POST /api/investigations/{incident_id}/resume` — Resume an investigation
- `GET /api/investigations/{incident_id}/events` — SSE stream of investigation events

### Cases
- `GET /api/cases/search` — Search verified cases
- `POST /api/cases` — Save a new case
- `POST /api/cases/{case_id}/confirm` — Confirm a case

### Scenarios
- `GET /api/scenarios` — List all scenario definitions
- `POST /api/scenarios/{name}/enable` — Activate a scenario
- `POST /api/scenarios/{name}/disable` — Deactivate a scenario
- `POST /api/scenarios/reset` — Reset all scenarios and demo data
- `GET /api/scenarios/runtime/{service}` — Get active scenarios for a service

### Health
- `GET /healthz` — Health check

## Fault Scenarios

| Scenario | Target | Root Cause |
|----------|--------|------------|
| payment_delay | payment-service | payment_latency_spike |
| payment_error_rate | payment-service | payment_service_degradation |
| db_pool_exhaustion | order-service | database_connection_leak |
| dependency_unavailable | order-service | network_partition |
| deployment_regression | payment-service | bad_deployment |

## Evaluation

See [docs/evaluation.md](docs/evaluation.md) for evaluation methodology.

## Demo Runner

The reusable demo runner orchestrates end-to-end scenarios via public APIs:

```bash
# Run a single scenario
uv run python scripts/run_demo.py --scenario payment_delay

# Run all scenarios
uv run python scripts/run_demo.py --all

# With custom URLs and traffic count
uv run python scripts/run_demo.py --all \
  --control-plane-url http://localhost:8003 \
  --gateway-url http://localhost:8000 \
  --traffic-count 5

# In Docker Compose mode (deterministic params)
uv run python scripts/run_demo.py --all --compose

# Reset demo state
uv run python scripts/reset_demo.py

# Generate traffic
uv run python scripts/generate_traffic.py --count 20 --url http://localhost:8000
```

## Constraints

- Python >=3.12,<3.13
- All tools are read-only (no write operations)
- Tool results always return ToolResult (never unhandled exceptions)
- Confidence > 0.70 requires evidence references
- Historical cases only generate candidate hypotheses (never confirmed)
- Root cause labels are NOT exposed via API (defense-in-depth)

## Phase 5: Knowledge Loop

Phase 5 introduces case memory and governance to enable historical learning:

### Case States

Cases follow a governed state machine:

| State | Description | Allowed Actions |
|-------|-------------|-----------------|
| `draft` | Initial state after creation or editing | edit, confirm, reject |
| `agent_generated` | Auto-materialized from investigation report | edit, confirm, reject |
| `human_verified` | Confirmed by reviewer and included in formal retrieval | edit, deprecate |
| `deprecated` | Removed from formal retrieval | edit for re-review |
| `rejected` | Rejected during review | edit for re-review |

### Auto-Materialization

When an investigation reaches `report_ready`, the system automatically creates a case in `agent_generated` state. This ensures every investigation produces a learnable artifact.

### Search and Retrieval

- **FTS5**: Always available, used for keyword-based search
- **Embeddings**: Optional semantic search with configurable provider
- **Hybrid retrieval**: Combines FTS5 and embedding scores with explanation

### Review Workflow

```bash
# Create a draft case
curl -X POST /api/cases \
  -H 'Content-Type: application/json' \
  -d '{"symptom":"payment timeout","affected_services":["order-service"],"actor":"local-user"}'

# Edit a case (creates new revision)
curl -X PATCH /api/cases/{id} \
  -H 'Content-Type: application/json' \
  -d '{"expected_version":1,"actor":"reviewer","symptom":"payment timeout","affected_services":["order-service"],"root_cause_category":"downstream-timeout","root_cause_description":"payment latency","key_evidence":[{"evidence_id":"ev-1"}],"resolution":"remove downstream delay"}'

# Confirm a case (enables FTS indexing)
curl -X POST /api/cases/{id}/confirm \
  -H 'Content-Type: application/json' \
  -d '{"expected_version":2,"actor":"reviewer","reason":"evidence checked"}'

# Reject a draft or agent-generated case
curl -X POST /api/cases/{id}/reject \
  -H 'Content-Type: application/json' \
  -d '{"expected_version":1,"actor":"reviewer","reason":"incorrect conclusion"}'

# Deprecate a verified case
curl -X POST /api/cases/{id}/deprecate \
  -H 'Content-Type: application/json' \
  -d '{"expected_version":3,"actor":"reviewer","reason":"no longer applicable"}'
```

### Feedback

Record feedback on case search results:

```bash
curl -X POST /api/cases/{id}/feedback -d '{
  "rating": "helpful",
  "actor": "local-user",
  "incident_id": "inc-123",
  "idempotency_key": "inc-123:feedback"
}'
```

### Export

Export investigation data with sanitization:

```bash
curl /api/investigations/{incident_id}/export
```

Exports include evidence references and exclude sensitive fields (API keys, tokens, root_cause_label).

### Evaluation

Run strategy comparisons:

```bash
# Run all strategies
python -m incidentlens_evaluation.cli --strategy all --scenario all

# Run single strategy
python -m incidentlens_evaluation.cli --strategy incidentlens_verified --scenario payment_delay
```

### Metrics

Eight metrics computed from actual run records:

| Metric | Description |
|--------|-------------|
| `root_service_accuracy` | Fraction of runs where identified service matches expected |
| `root_cause_type_accuracy` | Fraction of runs where cause type matches expected |
| `evidence_reference_correctness` | Percentage of runs with correct evidence references |
| `first_effective_hypothesis_round` | Average round where first effective hypothesis appears |
| `average_tool_calls` | Mean tool calls per investigation |
| `duplicate_rate` | Fraction of total calls that are duplicates |
| `historical_case_misleading_rate` | misleading adopted cases / adopted cases |
| `average_latency_ms` | Mean investigation latency |

### Dashboard

The control plane dashboard provides:
- Case list with status, revision, and last updated
- Case detail with full history and usage events
- Feedback submission and review
- Investigation export

### Project Boundaries

IncidentLens is a **development and debugging tool**, not a production incident response system. It:
- Uses read-only diagnostic tools
- Generates candidate hypotheses (never confirmed root causes)
- Requires human review for all case confirmations
- Does not perform automated remediation
