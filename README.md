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

## Constraints

- Python >=3.12,<3.13
- All tools are read-only (no write operations)
- Tool results always return ToolResult (never unhandled exceptions)
- Confidence > 0.70 requires evidence references
- Historical cases only generate candidate hypotheses (never confirmed)
- Root cause labels are NOT exposed via API (defense-in-depth)
