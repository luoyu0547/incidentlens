# Evaluation Methodology

## Overview

IncidentLens evaluation measures how effectively the investigation engine
identifies root causes across different fault scenarios and strategies.

## Strategies

| Strategy | Case Memory | Evidence Verification | Description |
|----------|-------------|----------------------|-------------|
| `react_no_memory` | No | No | Baseline: no historical cases, no evidence verification |
| `memory_unverified` | Yes | No | Case memory enabled, but evidence not verified |
| `incidentlens_verified` | Yes | Yes | Full pipeline: case memory + evidence verification |

## Scenarios

Five fault scenarios are evaluated:

1. **payment_delay** — Latency injection on payment-service
2. **payment_error_rate** — Error rate injection on payment-service
3. **db_pool_exhaustion** — Connection pool exhaustion on order-service
4. **dependency_unavailable** — Network partition between order and payment
5. **deployment_regression** — Buggy deployment on payment-service

## Metrics

All metrics are computed from actual run records — never fixed/hardcoded scores.

| Metric | Description | Formula |
|--------|-------------|---------|
| root_service_accuracy | Fraction of runs where identified service matches expected | correct / total |
| evidence_reference_correctness | Percentage of runs with correct evidence references | correct_refs / total * 100 |
| first_effective_hypothesis_round | Average round where first effective hypothesis appears | mean(rounds) |
| average_tool_calls | Mean tool calls per investigation | sum(calls) / total |
| duplicate_rate | Fraction of total calls that are duplicates | duplicates / total_calls |
| misleading_rate | Fraction of total calls that are misleading | misleading / total_calls |
| average_latency_ms | Mean investigation latency | sum(ms) / total |

## Running Evaluations

```python
from incidentlens_evaluation.runner import run_evaluation

# Run a single scenario
result = run_evaluation("incidentlens_verified", "payment_delay")

# Run all scenarios
result = run_evaluation("incidentlens_verified", "all")
```

## Compose Acceptance Validation

In addition to the strategy-comparison metrics above, five Compose acceptance
runs validate the pipeline end-to-end using Docker Compose with deterministic
parameters (`payment_error_rate=1.0` for the `payment_error_rate` scenario).

Each of the five scenarios is run via `DemoRunner(compose=True)` and must:

1. Produce a report with the expected `root_service`
2. Cite non-empty `evidence_ids` in the report
3. Never expose `root_cause_label` in any output

| Scenario | Expected root_service |
|----------|-----------------------|
| payment_delay | payment-service |
| payment_error_rate | payment-service |
| db_pool_exhaustion | order-service |
| dependency_unavailable | order-service |
| deployment_regression | payment-service |

Run the acceptance tests:

```bash
docker compose -f infra/compose/compose.yaml up --build
uv run pytest tests/integration/test_scenario_acceptance.py -q
```

These acceptance runs are independent of the strategy-comparison evaluation
metrics and serve as a quality gate for the full investigation pipeline.

## Expected Outcomes

The `incidentlens_verified` strategy should outperform `react_no_memory` on:
- Higher root_service_accuracy (case memory guides investigation)
- Lower duplicate_rate (evidence dedup prevents redundant calls)
- Lower misleading_rate (evidence verification filters bad signals)
- Earlier first_effective_hypothesis_round (historical cases accelerate)

The `memory_unverified` strategy should fall between the other two,
benefiting from case memory but lacking evidence verification.
