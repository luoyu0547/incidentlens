---
name: downstream-timeout
description: Diagnose upstream latency and 5xx caused by slow downstream spans or timeout behavior. Use when traces or logs indicate a slow dependency.
license: MIT
compatibility: IncidentLens phase 3; read-only observability tools
metadata:
  version: "1.0.0"
allowed-tools: read_file get_service_dependencies get_slow_traces get_trace search_logs query_metrics get_runbook
---

# Downstream timeout investigation

## Applicable symptoms
- Upstream P95 latency and 5xx rise in the same incident window.
- One downstream span accounts for most of the slow trace duration.

## Investigation order
1. Read `references/trace-latency-guide.md`.
2. Query service dependencies and identify the downstream edge.
3. Query slow traces, then inspect a representative trace.
4. Correlate timeout logs and latency/error metrics for the same time window.

## Candidate hypothesis
The upstream failure is caused by latency or timeout behavior in a downstream service.

## Minimum supporting evidence
- A current-incident slow trace identifies the downstream span.
- A second independent current source is abnormal: correlated logs or metrics.
- A report may cite only Evidence IDs returned by executed tools.

## Contradictions
- Downstream span latency is normal in the incident window.
- The delay occurs entirely before or after the downstream call.

## Stop conditions
- Stop with insufficient evidence when two independent current sources cannot be obtained.
- Stop and reject this hypothesis when a direct contradiction remains unresolved.

## Forbidden behavior
- Do not treat a historical case as current proof.
- Do not invent Evidence IDs or claim an unexecuted query.
- Do not request writes, Shell, restarts, rollbacks, or configuration changes.
