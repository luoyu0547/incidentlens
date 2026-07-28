---
name: deployment-regression
description: Diagnose a failure correlated with a recent version or configuration deployment. Use only when current telemetry and a change record align.
license: MIT
compatibility: IncidentLens phase 3; read-only observability tools
metadata:
  version: "1.0.0"
allowed-tools: read_file list_recent_deployments search_logs query_metrics get_slow_traces get_trace get_runbook
---

# Deployment regression investigation

## Applicable symptoms
- Failure onset aligns temporally with a recent deployment or configuration change.
- Error patterns or latency characteristics appear only in the new version.
- A deployment event is present in the change log within the incident window.

## Investigation order
1. Read `references/change-correlation-guide.md`.
2. List recent deployments and identify candidate changes.
3. Search logs for error patterns that correlate with the deployment timestamp.
4. Query metrics and slow traces to compare pre-deployment and post-deployment behavior.

## Candidate hypothesis
The failure is caused by a regression introduced by a recent version or configuration deployment.

## Minimum supporting evidence
- A current-incident deployment record aligns temporally with the failure onset.
- A second independent current source shows behavioral change: correlated logs or metrics.
- A report may cite only Evidence IDs returned by executed tools.

## Contradictions
- The same failure predates the candidate deployment.
- The failure pattern is identical before and after the deployment.

## Stop conditions
- Stop with insufficient evidence when two independent current sources cannot be obtained.
- Stop and reject this hypothesis when a direct contradiction remains unresolved.

## Forbidden behavior
- Do not treat a historical case as current proof.
- Do not invent Evidence IDs or claim an unexecuted query.
- Do not request writes, Shell, restarts, rollbacks, or configuration changes.
