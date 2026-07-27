# Task 3 Report: Three-Service Call Chain & Fault Scenarios

## What Was Implemented

### Three FastAPI Services (Call Chain: Gateway -> Order -> Payment)

1. **Gateway Service** (`apps/gateway-service/src/gateway_service/main.py`)
   - `POST /orders` - proxies to order-service, propagates X-Request-ID and X-Trace-ID
   - `GET /healthz` - returns `{"status": "ok"}`
   - Emits span, log, and metric telemetry events

2. **Order Service** (`apps/order-service/src/order_service/main.py`)
   - `POST /orders` - creates order, calls payment-service for charge
   - `GET /healthz` - returns `{"status": "ok"}`
   - Applies `db_pool_exhaustion` and `dependency_unavailable` fault scenarios
   - Emits span, log, and metric telemetry events

3. **Payment Service** (`apps/payment-service/src/payment_service/main.py`)
   - `POST /charge` - processes payment charges
   - `GET /healthz` - returns `{"status": "ok"}`
   - Applies `payment_delay`, `payment_error_rate`, and `deployment_regression` fault scenarios
   - Emits span, log, and metric telemetry events

### Shared Service Common Code (`apps/shared-service/src/incidentlens_service_common/`)

- **context.py** - `extract_context()`, `propagate_headers()`, `generate_request_id()`, `generate_trace_id()`
- **telemetry_client.py** - `TelemetryClient` with `emit_log()`, `emit_metric()`, `emit_span()` methods

### Scenarios Package (`packages/scenarios/src/incidentlens_scenarios/`)

- **models.py** - `SCENARIOS` dict with 5 fault definitions, each with `target_service`, `root_cause_label`, and `default_params`
- **service.py** - `ScenarioService` with `enable()`, `disable()`, `reset()`, `active_for()`, `is_active()`, `get_params()`

### Five Fault Scenarios

| Scenario | Target Service | Root Cause Label | Behavior |
|---|---|---|---|
| `payment_delay` | payment-service | payment_latency_spike | Adds real `asyncio.sleep()` delay |
| `payment_error_rate` | payment-service | payment_service_degradation | Returns HTTP 500 at configured rate |
| `db_pool_exhaustion` | order-service | database_connection_leak | Simulates slow DB with 0.5s delay |
| `dependency_unavailable` | order-service | network_partition | Returns HTTP 502 without calling payment |
| `deployment_regression` | payment-service | bad_deployment | Returns approved charge with amount=0 |

### Workspace Configuration

- Added 5 new workspace members to root `pyproject.toml`
- Added `pytest-asyncio>=0.24` to dev dependencies
- Set `asyncio_mode = "auto"` in pytest config
- Each new package has its own `pyproject.toml` with proper workspace dependencies

## What Was Tested and Test Results

### Test Files

1. `tests/services/test_request_flow.py` - 8 tests
   - 3 health check tests (one per service)
   - 5 call chain tests (trace propagation, auto-generation, order details, direct payment, direct order)

2. `tests/scenarios/test_lifecycle.py` - 14 tests
   - 9 lifecycle tests (enable 5 scenarios, disable, reset, empty, unknown raises)
   - 2 root cause isolation tests (not exposed in API, stored internally)
   - 3 fault behavior tests (real delay, error rate 500, dependency 502)

### Test Results

```
53 passed in 1.72s  (22 new + 31 existing)
```

All tests pass, no regressions.

## TDD Evidence

### RED Phase
- Wrote all 22 tests first before any implementation code existed
- Ran `uv run pytest tests/services/test_request_flow.py tests/scenarios/test_lifecycle.py -q`
- Result: 11 FAILED, 11 ERRORS (ModuleNotFoundError for missing modules)

### GREEN Phase
- Implemented all packages and services
- Ran same test command
- Result: 22 passed

### REFACTOR Phase
- Removed unused `Request` parameter from service endpoints
- Removed unused `generate_trace_id` import from payment service
- Removed unused `Any` import from context.py
- Fixed import ordering via ruff
- All 53 tests still pass after refactoring

## Files Changed

### New Files (20)
- `apps/gateway-service/pyproject.toml`
- `apps/gateway-service/src/gateway_service/__init__.py`
- `apps/gateway-service/src/gateway_service/main.py`
- `apps/order-service/pyproject.toml`
- `apps/order-service/src/order_service/__init__.py`
- `apps/order-service/src/order_service/main.py`
- `apps/payment-service/pyproject.toml`
- `apps/payment-service/src/payment_service/__init__.py`
- `apps/payment-service/src/payment_service/main.py`
- `apps/shared-service/pyproject.toml`
- `apps/shared-service/src/incidentlens_service_common/__init__.py`
- `apps/shared-service/src/incidentlens_service_common/context.py`
- `apps/shared-service/src/incidentlens_service_common/telemetry_client.py`
- `packages/scenarios/pyproject.toml`
- `packages/scenarios/src/incidentlens_scenarios/__init__.py`
- `packages/scenarios/src/incidentlens_scenarios/models.py`
- `packages/scenarios/src/incidentlens_scenarios/service.py`
- `tests/scenarios/__init__.py`
- `tests/scenarios/test_lifecycle.py`
- `tests/services/__init__.py`
- `tests/services/test_request_flow.py`

### Modified Files (2)
- `pyproject.toml` - added workspace members, source mappings, pytest-asyncio
- `uv.lock` - updated lockfile

## Self-Review Findings

1. **Context propagation works correctly**: X-Trace-ID and X-Request-ID are extracted from incoming headers, generated if missing, and propagated to downstream services via `propagate_headers()`.

2. **Root cause labels are properly isolated**: The `SCENARIOS` dict stores `root_cause_label` internally, but `active_for()` explicitly filters it out. Tests verify both that labels exist internally and are not leaked.

3. **Fault scenarios actually change behavior**: Each fault type has a real, observable effect (delays, error responses, wrong data). The `payment_error_rate` uses `random.random()` which is non-deterministic, but the test uses `error_rate=1.0` for deterministic behavior.

4. **Service-to-service calls use ASGI transport**: In the current implementation, services call each other via `httpx.ASGITransport` for in-process testing. For production deployment, this would need to be changed to actual HTTP calls using the configured service URLs.

5. **Scenario service injection uses module-level global**: The `set_scenario_service()` function sets a module-level `_scenario_service` variable. This is simple and works for testing but is not thread-safe for concurrent production use. This is acceptable for the current phase since the control plane (Task 4) will manage scenario lifecycle.

6. **Minor concern**: The `payment_error_rate` fault with `error_rate < 1.0` is probabilistic. Tests only verify the deterministic case (`error_rate=1.0`). A more robust test could seed the random number generator, but this is adequate for the current scope.

## Issues or Concerns

- None blocking. The implementation meets all acceptance criteria from the task brief.

---

## Task 3 Review Fixes

### Fix 1: `get_params()` root_cause_label filtering (defense-in-depth)

**File**: `packages/scenarios/src/incidentlens_scenarios/service.py`

`get_params()` previously returned the raw `_active[name]` dict without filtering `root_cause_label`. Now it applies the same filter as `active_for()`, excluding `root_cause_label` from the returned dict. A corresponding test (`test_get_params_does_not_expose_root_cause`) was added.

### Fix 2: `db_pool_exhaustion` parameterized delay

**File**: `apps/order-service/src/order_service/main.py`

The scenario previously hardcoded `asyncio.sleep(0.5)`. Now it uses the `pool_size` parameter to influence the delay: `delay = max(0.1, 1.0 / pool_size)`. For example, `pool_size=2` gives 0.5s, `pool_size=1` gives 1.0s, `pool_size=4` gives 0.25s. A test (`test_db_pool_exhaustion_causes_delay`) was added.

### Fix 3: Missing fault behavior tests

**File**: `tests/scenarios/test_lifecycle.py`

Two new tests added:
1. `test_db_pool_exhaustion_causes_delay` — enables the fault, calls the order endpoint, verifies increased latency matching the parameterized delay.
2. `test_deployment_regression_returns_zero_amount` — enables the fault, calls the payment endpoint, verifies `amount=0` in the response.

### Fix 4: Configurable transport (ASGI vs HTTP)

**Files**: `apps/gateway-service/src/gateway_service/main.py`, `apps/order-service/src/order_service/main.py`

Inter-service calls now read `ORDER_SERVICE_URL` / `PAYMENT_SERVICE_URL` from environment variables. When set (non-empty), real HTTP transport is used (suitable for Docker/production). When empty, ASGI transport is used (fast in-process testing). Test files patch these module-level constants to empty strings so ASGI transport is used during testing.

### Minor fixes

1. **Module-level imports**: Moved `import asyncio`, `import random`, `import os`, and `from fastapi.responses import JSONResponse` from inside function bodies to module-level imports in order-service and payment-service.
2. **X-Request-ID propagation test**: Added `test_request_id_propagates_across_hops` in `tests/services/test_request_flow.py` verifying the full call chain preserves headers.
3. **`get_params` isolation test**: Added `test_get_params_does_not_expose_root_cause` in `tests/scenarios/test_lifecycle.py`.

### Test Results After Fixes

```
uv run pytest tests/services/test_request_flow.py tests/scenarios/test_lifecycle.py -q
26 passed in 1.64s
```

```
uv run pytest -q
57 passed in 1.78s
```

All tests pass with no regressions.
