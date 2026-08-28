# Controlled Dual-Regression Acceptance Target

This Compose environment is a deterministic controlled scenario, not a production deployment.

```bash
docker compose -f infra/acceptance/docker-compose.yml \
  -f infra/acceptance/compose.cloud.yaml up -d --build
python infra/acceptance/scripts/request_matrix.py --expected pre-repair
```

The public gateway is loopback-bound. `route-a` selects the stable replica and
`route-b` selects the canary replica. The request matrix emits one JSON object
per route/amount cell and exits nonzero when the expected state is not observed.

The runtime scenario definition contains no diagnostic labels or expected root
causes. Keep answer-bearing test fixtures outside the registered remote paths
provided to an investigation.
