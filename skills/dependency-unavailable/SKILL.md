---
name: dependency-unavailable
description: Diagnose an unreachable service or network dependency. Use when connection failures and broken dependency traces occur.
license: MIT
compatibility: IncidentLens phase 3; read-only observability tools
metadata:
  version: "1.0.0"
allowed-tools: read_file get_service_dependencies search_logs query_metrics get_trace get_runbook
---

# Dependency unavailable investigation

## Applicable symptoms
- Requests to a downstream service fail with connection refused, DNS resolution failure, or network timeout errors.
- Traces show missing or incomplete downstream spans.
- Dependency health checks report the target service as down.

## Investigation order
1. Read `references/dependency-health-guide.md`.
2. Query service dependencies and identify the unreachable target.
3. Search logs for connection failure or network error messages.
4. Query health-check and connectivity metrics for the dependency.

## Candidate hypothesis
The failure is caused by an unreachable service or broken network dependency that the upstream service depends on.

## Minimum supporting evidence
- A current-incident log entry shows connection failure or network error for the dependency.
- A second independent current source is abnormal: dependency health metrics or broken trace spans.
- A report may cite only Evidence IDs returned by executed tools.

## Contradictions
- Successful dependency calls continue through the same incident window.
- The target service responds normally to health checks during the failure period.

## Stop conditions
- Stop with insufficient evidence when two independent current sources cannot be obtained.
- Stop and reject this hypothesis when a direct contradiction remains unresolved.

## Forbidden behavior
- Do not treat a historical case as current proof.
- Do not invent Evidence IDs or claim an unexecuted query.
- Do not request writes, Shell, restarts, rollbacks, or configuration changes.
