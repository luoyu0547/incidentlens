---
name: database-pool-exhaustion
description: Diagnose request failures caused by database connection acquisition saturation. Use when pool timeout logs or pool metrics are present.
license: MIT
compatibility: IncidentLens phase 3; read-only observability tools
metadata:
  version: "1.0.0"
allowed-tools: read_file search_logs query_metrics get_slow_traces get_trace get_runbook
---

# Database pool exhaustion investigation

## Applicable symptoms
- Requests fail with connection acquisition timeout errors.
- Database connection pool utilization is at or near 100% during the incident window.
- Slow queries hold connections longer than normal, reducing available pool capacity.

## Investigation order
1. Read `references/pool-saturation-guide.md`.
2. Search logs for connection pool timeout or exhaustion messages.
3. Query pool utilization metrics and identify the saturation window.
4. Query slow traces to find queries holding connections for extended durations.

## Candidate hypothesis
The failure is caused by database connection pool exhaustion, where connections are acquired faster than they are released.

## Minimum supporting evidence
- A current-incident log entry shows pool timeout or exhaustion.
- A second independent current source is abnormal: pool metrics or slow trace data.
- A report may cite only Evidence IDs returned by executed tools.

## Contradictions
- Available pool capacity remains healthy during failed requests.
- The connection count is well below the configured maximum during the incident.

## Stop conditions
- Stop with insufficient evidence when two independent current sources cannot be obtained.
- Stop and reject this hypothesis when a direct contradiction remains unresolved.

## Forbidden behavior
- Do not treat a historical case as current proof.
- Do not invent Evidence IDs or claim an unexecuted query.
- Do not request writes, Shell, restarts, rollbacks, or configuration changes.
