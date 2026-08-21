# Dual Regression Acceptance Scenario Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a deterministic two-root-cause Docker target which requires cross-service correlation and supports approved repair and rollback.

**Architecture:** The gateway deterministically routes an explicit opaque test correlation key to stable/canary order replicas. A canary database-port drift and an independent high-value payment policy regression produce distinct failures while every health endpoint remains green.

**Tech Stack:** Docker Compose, Flask, PostgreSQL, pytest, HTTP requests.

**Spec:** `docs/superpowers/specs/2026-08-21-hard-cloud-incident-terminal-design.md`

## Global Constraints

- Depends on the runtime plan before live Agent evaluation, but the target is independently testable.
- Fault manifests and expected answers are never mounted into a target path registered for Agent reads.
- Routing and request matrices are deterministic; no random failure injection.
- Cloud ports bind to `127.0.0.1` only.

---

### Task 1: Stable/canary routing with correlation IDs

**Files:**
- Modify: `infra/acceptance/docker-compose.yml`
- Modify: `infra/acceptance/services/api-gateway/app.py`
- Modify: `infra/acceptance/services/order-service/app.py`
- Test: `tests/acceptance/test_docker_scenarios.py`

**Interfaces:**
- Consumes request header `X-Incident-Test-Route: stable|canary` and `X-Request-ID`.
- Produces correlated gateway/order logs and deterministic replica routing.

- [ ] **Step 1: Add failing Docker matrix tests**

```python
stable = post_order(route="stable", amount=20, request_id="req-stable-low")
canary = post_order(route="canary", amount=20, request_id="req-canary-low")
assert stable.headers["X-Served-By"] == "order-stable"
assert canary.headers["X-Served-By"] == "order-canary"
```

- [ ] **Step 2: Run the focused acceptance test**

Run: `uv run pytest tests/acceptance/test_docker_scenarios.py -k routing -v`

Expected: FAIL because there is one order service and no route header.

- [ ] **Step 3: Implement two services and bounded correlation logging**

Do not log request bodies or credentials. Include request ID, replica and status in gateway/order lines. Keep `/health` green on both replicas.

- [ ] **Step 4: Run the routing matrix**

Run: `uv run pytest tests/acceptance/test_docker_scenarios.py -k routing -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/acceptance tests/acceptance/test_docker_scenarios.py
git commit -m "feat(acceptance): add deterministic stable canary routing"
```

### Task 2: Two non-leaking deployment regressions

**Files:**
- Create: `infra/acceptance/config/order-canary.env`
- Create: `infra/acceptance/config/payment-policy.env`
- Modify: `infra/acceptance/services/payment-service/app.py`
- Create: `infra/acceptance/scenarios/dual-deployment-regression.yaml`
- Test: `tests/acceptance/test_docker_scenarios.py`

**Interfaces:**
- Fault A: canary `DB_PORT=5433` while expected is `5432`.
- Fault B: payment `HIGH_VALUE_LIMIT=50` while expected is `5000`.

- [ ] **Step 1: Add failing pre-repair matrix tests**

```python
assert post_order("stable", 20).status_code == 201
assert post_order("stable", 500).status_code in {429, 503}
assert post_order("canary", 20).status_code == 500
assert post_order("canary", 500).status_code == 500
assert all(get_health(name).status_code == 200 for name in SERVICES)
```

- [ ] **Step 2: Verify the matrix does not yet exist**

Run: `uv run pytest tests/acceptance/test_docker_scenarios.py -k dual_regression -v`

Expected: FAIL.

- [ ] **Step 3: Implement fault behavior without root-cause labels**

Payment logs `request_id`, `policy_version`, decision and status. It must not print the expected threshold. Store answer metadata outside registered remote read scopes.

- [ ] **Step 4: Run health and pre-repair matrix tests**

Run: `uv run pytest tests/acceptance/test_docker_scenarios.py -k 'dual_regression or health' -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/acceptance tests/acceptance/test_docker_scenarios.py
git commit -m "feat(acceptance): add dual deployment regression"
```

### Task 3: Reversible repair surface and verification driver

**Files:**
- Create: `infra/acceptance/scripts/request_matrix.py`
- Create: `infra/acceptance/compose.cloud.yaml`
- Modify: `infra/acceptance/README.md`
- Test: `tests/acceptance/test_request_matrix.py`

**Interfaces:**
- Produces: `run_matrix(base_url: str) -> MatrixResult` with four named cells and process exit 0 only when all expected statuses match selected mode.

- [ ] **Step 1: Add failing parser and matrix-result tests**

```python
assert result.cells["stable.normal"].status == 201
assert result.cells["canary.high"].request_id == "matrix-canary-high"
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/acceptance/test_request_matrix.py -v`

Expected: FAIL because the driver is absent.

- [ ] **Step 3: Implement driver and cloud override**

The driver emits JSON Lines with request ID, route, amount, status and served-by. The cloud override binds every published port to `127.0.0.1`. Repairable env files live under the registered protected host scope so file changes require exact approval and backup.

- [ ] **Step 4: Exercise pre-repair, repaired and rollback matrices locally**

Run: `docker compose -f infra/acceptance/docker-compose.yml up -d --build && uv run pytest tests/acceptance -k 'request_matrix or dual_regression' -v`

Expected: PASS and deterministic statuses on three consecutive runs.

- [ ] **Step 5: Commit**

```bash
git add infra/acceptance tests/acceptance
git commit -m "test(acceptance): add reversible four-path verification matrix"
```

