---
name: downstream-error
description: Diagnose upstream failures caused by an elevated downstream application error rate. Use when dependency spans and logs contain correlated errors.
license: MIT
compatibility: IncidentLens phase 3; read-only observability tools
metadata:
  version: "1.0.0"
allowed-tools: read_file get_service_dependencies get_trace search_logs query_metrics get_runbook
---

# Downstream error investigation

## Applicable symptoms
- Upstream error rate rises in the same incident window as elevated downstream errors.
- Dependency spans show 5xx responses or application-level exceptions.

## Investigation order
1. Read `references/error-correlation-guide.md`.
2. Query service dependencies and identify the failing downstream edge.
3. Query traces to inspect error spans and their error signatures.
4. Correlate error logs and error-rate metrics for the same time window.

## Candidate hypothesis
The upstream failure is caused by an elevated application error rate in a downstream service.

## Minimum supporting evidence
- A current-incident trace shows downstream error spans correlated with upstream failures.
- A second independent current source is abnormal: correlated logs or error-rate metrics.
- A report may cite only Evidence IDs returned by executed tools.

## Contradictions
- Downstream success and error rates remain at baseline.
- The upstream errors predate or postdate the downstream errors.

## Stop conditions
- Stop with insufficient evidence when two independent current sources cannot be obtained.
- Stop and reject this hypothesis when a direct contradiction remains unresolved.

## Forbidden behavior
- Do not treat a historical case as current proof.
- Do not invent Evidence IDs or claim an unexecuted query.
- Do not request writes, Shell, restarts, rollbacks, or configuration changes.
