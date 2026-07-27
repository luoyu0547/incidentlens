# IncidentLens Phase 2 End-to-End Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Deliver a reproducible five-scenario Compose demo where each investigation returns the expected root service and current incident Evidence IDs.

**Architecture:** The control plane owns persistent scenario state and exposes only safe runtime parameters. Services fetch it per request and send real telemetry to the control plane. A reusable DemoRunner drives public APIs for the CLI and Compose tests; deterministic evidence rules generate guarded reports.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, SQLite, httpx, pytest, Docker Compose, argparse.

## Global Constraints

- Python is >=3.12,<3.13; use uv for every command.
- The control plane is the sole persistent scenario authority; services have no Compose-mode process-local scenario state.
- root_cause_label must never be returned by APIs, tools, telemetry, reports, CLI output, audit records, or the Web page.
- Tools remain read-only and return ToolResult for expected failures.
- A report requires a root_service and non-empty current-incident evidence_ids.
- Historical cases generate candidates only; current telemetry is necessary for confirmation.
- Each run starts and ends with an API reset, never a volume or SQLite-file deletion.
- Compose checks use payment_error_rate=1.0 for deterministic results.

---

## File Structure

| Path | Responsibility |
| --- | --- |
| packages/scenarios/src/incidentlens_scenarios/{models.py,store.py} | Public models, persistent state, safe runtime projection. |
| apps/control-plane/src/incidentlens_control_plane/routes/scenarios.py | Scenario lifecycle/runtime HTTP API. |
| apps/control-plane/src/incidentlens_control_plane/services/demo_reset.py | Transactional demo-data cleanup. |
| apps/shared-service/src/incidentlens_service_common/{runtime_client.py,telemetry_client.py} | Bounded config fetch and async telemetry delivery. |
| apps/control-plane/src/incidentlens_control_plane/agent/evidence_rules.py | Deterministic evidence-to-hypothesis mappings. |
| packages/demo/src/incidentlens_demo/runner.py | Shared public-API scenario orchestration. |
| scripts/run_demo.py | Interactive runner CLI. |
| tests/{scenarios,services,agent,demo,integration}/ | Unit, contract, CLI, and Compose acceptance tests. |

### Task 1: Persistent Scenario Control Plane

**Files:**
- Modify: packages/scenarios/src/incidentlens_scenarios/models.py, packages/scenarios/src/incidentlens_scenarios/__init__.py, packages/telemetry/src/incidentlens_telemetry/repository.py
- Create: packages/scenarios/src/incidentlens_scenarios/store.py, apps/control-plane/src/incidentlens_control_plane/routes/scenarios.py, apps/control-plane/src/incidentlens_control_plane/services/__init__.py, apps/control-plane/src/incidentlens_control_plane/services/demo_reset.py
- Modify: apps/control-plane/src/incidentlens_control_plane/main.py
- Test: tests/scenarios/test_store.py, tests/scenarios/test_api.py

**Interfaces:**
- Produces ScenarioStore(engine), enable(name, params), disable(name), runtime_for(service) -> dict[str, dict[str, Any]], and reset_demo_data() -> None.
- Produces GET /api/scenarios, POST /api/scenarios/{name}/enable, POST /api/scenarios/{name}/disable, POST /api/scenarios/reset, and GET /api/scenarios/runtime/{service}.

- [ ] **Step 1: Write failing store and API tests**

~~~python
def test_runtime_projection_excludes_internal_label(store) -> None:
    store.enable("payment_delay", {"delay_ms": 250})
    assert store.runtime_for("payment-service") == {"payment_delay": {"delay_ms": 250}}
    assert "root_cause_label" not in repr(store.runtime_for("payment-service"))

async def test_enable_and_reset_are_publicly_observable(client) -> None:
    enabled = await client.post("/api/scenarios/payment_error_rate/enable", json={"error_rate": 1.0})
    assert enabled.status_code == 200 and "root_cause_label" not in enabled.text
    assert (await client.get("/api/scenarios/runtime/payment-service")).json()["active"]
    assert (await client.post("/api/scenarios/reset")).status_code == 200
~~~

- [ ] **Step 2: Run test to verify failure**

Run: uv run pytest tests/scenarios/test_store.py tests/scenarios/test_api.py -q

Expected: FAIL because ScenarioStore and the scenario routes do not exist.

- [ ] **Step 3: Implement state, validation, and reset**

~~~python
class ScenarioStore:
    def runtime_for(self, service: str) -> dict[str, dict[str, Any]]:
        return {row.name: dict(row.parameters) for row in self._active_rows(service)}

@router.get("/runtime/{service}")
async def runtime(service: str) -> RuntimeScenarioResponse:
    return RuntimeScenarioResponse(service=service, active=_store.runtime_for(service))
~~~

Define Pydantic enable requests per scenario. Unknown scenarios return 404 and invalid parameter ranges return 422. Persist active parameters; reset first clears scenarios, then deletes demo telemetry, checkpoints, investigations, and tool audits through repository methods.

- [ ] **Step 4: Register routes and dependencies**

~~~python
_scenario_store = ScenarioStore(_engine)
set_scenario_store(_scenario_store)
set_demo_reset_service(DemoResetService(_repository, _scenario_store))
app.include_router(scenarios_router)
~~~

Register before the static-files mount.

- [ ] **Step 5: Verify and commit**

Run: uv run pytest tests/scenarios/test_store.py tests/scenarios/test_api.py tests/telemetry/test_repository.py -q

Expected: PASS; a new store instance reads persisted state and all public JSON excludes the internal label.

~~~bash
git add packages/scenarios packages/telemetry apps/control-plane/src/incidentlens_control_plane tests/scenarios tests/telemetry
git commit -m "feat: add persistent scenario control API"
~~~

### Task 2: Runtime Configuration and Real Telemetry Delivery

**Files:**
- Create: apps/shared-service/src/incidentlens_service_common/runtime_client.py
- Modify: apps/shared-service/src/incidentlens_service_common/__init__.py, apps/shared-service/src/incidentlens_service_common/telemetry_client.py, apps/gateway-service/src/gateway_service/main.py, apps/order-service/src/order_service/main.py, apps/payment-service/src/payment_service/main.py, infra/compose/compose.yaml
- Test: tests/services/test_runtime_configuration.py, tests/services/test_telemetry_delivery.py

**Interfaces:**
- Produces RuntimeConfigClient(control_plane_url, service).get_active() -> dict[str, dict[str, Any]].
- Services consume CONTROL_PLANE_URL and use the returned map in Compose mode; existing setters remain for in-process tests.

- [ ] **Step 1: Write failing runtime and telemetry tests**

~~~python
async def test_runtime_client_returns_empty_config_on_timeout(httpx_mock) -> None:
    httpx_mock.add_exception(httpx.ConnectTimeout("control plane unavailable"))
    assert await RuntimeConfigClient("http://cp", "payment-service").get_active() == {}

async def test_payment_fault_telemetry_reaches_control_plane(control_plane, payment_client) -> None:
    await control_plane.post("/api/scenarios/payment_error_rate/enable", json={"error_rate": 1.0})
    assert (await payment_client.post("/charge", json={"amount": 1000})).status_code == 500
    logs = await control_plane.get("/api/telemetry/logs", params={"service": "payment-service"})
    assert "injected error" in logs.text
~~~

- [ ] **Step 2: Run test to verify failure**

Run: uv run pytest tests/services/test_runtime_configuration.py tests/services/test_telemetry_delivery.py -q

Expected: FAIL because runtime fetch and async HTTP telemetry are absent.

- [ ] **Step 3: Implement bounded clients and service behavior**

~~~python
async def get_active(self) -> dict[str, dict[str, Any]]:
    try:
        response = await self._client.get(f"/api/scenarios/runtime/{self.service}")
        response.raise_for_status()
        return response.json()["active"]
    except httpx.HTTPError:
        return {}
~~~

Give telemetry posts a short timeout and catch httpx.HTTPError after emitting a local diagnostic. Await telemetry on normal and abnormal paths; include duration, error type, and span status. Fetch runtime config at the start of each request and use it for all five faults.

- [ ] **Step 4: Wire Compose**

~~~yaml
environment:
  - CONTROL_PLANE_URL=http://control-plane:8003
depends_on:
  control-plane:
    condition: service_healthy
~~~

Apply to gateway, order, and payment without removing downstream health dependencies.

- [ ] **Step 5: Verify and commit**

Run: uv run pytest tests/services/test_runtime_configuration.py tests/services/test_telemetry_delivery.py tests/services/test_request_flow.py tests/scenarios/test_lifecycle.py -q

Expected: PASS; unavailable control plane means no injected fault, while an enabled fault produces persisted telemetry.

~~~bash
git add apps/shared-service apps/gateway-service apps/order-service apps/payment-service infra/compose tests/services
git commit -m "feat: wire services to runtime scenarios and telemetry"
~~~

### Task 3: Evidence Rules and Guarded Root-Service Reports

**Files:**
- Create: apps/control-plane/src/incidentlens_control_plane/agent/evidence_rules.py
- Modify: apps/control-plane/src/incidentlens_control_plane/agent/engine.py, apps/control-plane/src/incidentlens_control_plane/agent/reporting.py, apps/control-plane/src/incidentlens_control_plane/agent/state.py, apps/control-plane/src/incidentlens_control_plane/routes/investigations.py
- Test: tests/agent/test_evidence_rules.py, tests/agent/test_investigation_engine.py

**Interfaces:**
- Produces EvidenceAssessment(candidate_service, root_cause, supports, contradicts) and assess_evidence(evidence) -> list[EvidenceAssessment].
- Reports contain root_service, root_cause, evidence_ids, findings, rounds_completed, and uncertainty.
- The engine consumes persisted Evidence only; it may not import scenario definitions.

- [ ] **Step 1: Write failing evidence and report-guard tests**

~~~python
def test_payment_error_log_creates_payment_candidate() -> None:
    evidence = Evidence(source_tool="search_logs", tool_call_id="call-1", content={"items": [{"service": "payment-service", "level": "ERROR", "message": "Payment failed due to injected error rate"}]})
    assert assess_evidence(evidence)[0].candidate_service == "payment-service"

def test_report_rejects_evidence_not_owned_by_incident(state) -> None:
    state.hypotheses[0].supporting_evidence_ids = ["not-in-this-incident"]
    assert can_generate_report(state) is False
~~~

- [ ] **Step 2: Run test to verify failure**

Run: uv run pytest tests/agent/test_evidence_rules.py tests/agent/test_investigation_engine.py -q

Expected: FAIL because semantic assessments and root-service report fields are absent.

- [ ] **Step 3: Implement deterministic mappings and action selection**

~~~python
def assess_evidence(evidence: Evidence) -> list[EvidenceAssessment]:
    items = evidence.content.get("items", [])
    return [assessment for item in items for assessment in _assess_item(item, evidence.source_tool)]
~~~

Implement the five mappings: payment latency, payment errors, order pool exhaustion, order dependency failure, and payment deployment regression. Merge/update hypotheses by service and cause code. Query the alerted service first, then dependency, trace, or deployment evidence indicated by previous results.

- [ ] **Step 4: Implement report guard and response schema**

~~~python
def can_generate_report(state: InvestigationState) -> bool:
    confirmed = [h for h in state.hypotheses if h.status == HypothesisStatus.CONFIRMED]
    owned = {e.id for e in state.evidence}
    return bool(confirmed and confirmed[0].root_service and set(confirmed[0].supporting_evidence_ids) <= owned)
~~~

Add root_service and cause_code to hypothesis/state rather than parsing prose. Preserve SSE report events with the extended schema.

- [ ] **Step 5: Verify and commit**

Run: uv run pytest tests/agent/test_evidence_rules.py tests/agent/test_investigation_engine.py tests/tools/test_read_only_tools.py -q

Expected: PASS; each pattern maps to the expected service and foreign evidence prevents report generation.

~~~bash
git add apps/control-plane/src/incidentlens_control_plane/agent apps/control-plane/src/incidentlens_control_plane/routes/investigations.py tests/agent
git commit -m "feat: derive guarded root-service reports from evidence"
~~~

### Task 4: Reusable Demo Runner and Human CLI

**Files:**
- Create: packages/demo/pyproject.toml, packages/demo/src/incidentlens_demo/__init__.py, packages/demo/src/incidentlens_demo/runner.py, scripts/run_demo.py
- Modify: scripts/generate_traffic.py, scripts/reset_demo.py, pyproject.toml, README.md
- Test: tests/demo/test_runner.py, tests/demo/test_run_demo_cli.py

**Interfaces:**
- Produces DemoRunner(control_plane_url, gateway_url, traffic_count).run(scenario) -> DemoRunResult and run_all() -> list[DemoRunResult].
- DemoRunResult contains scenario, status, incident_id, trace_ids, report, failure_stage, and failure_message.
- CLI accepts mutually exclusive --scenario NAME and --all plus URL and traffic-count options.

- [ ] **Step 1: Write failing runner and CLI tests**

~~~python
async def test_runner_uses_public_api_and_returns_report(mock_api) -> None:
    result = await DemoRunner("http://control", "http://gateway", traffic_count=3).run("payment_delay")
    assert result.status == "passed"
    assert result.report["root_service"] == "payment-service"
    assert result.report["evidence_ids"]

def test_cli_all_prints_each_scenario(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_demo, "DemoRunner", FakePassingRunner)
    assert run_demo.main(["--all"]) == 0
    assert "deployment_regression: passed" in capsys.readouterr().out
~~~

- [ ] **Step 2: Run test to verify failure**

Run: uv run pytest tests/demo/test_runner.py tests/demo/test_run_demo_cli.py -q

Expected: FAIL because the demo package and CLI do not exist.

- [ ] **Step 3: Implement public-API orchestration**

~~~python
async def run(self, scenario: str) -> DemoRunResult:
    await self._post("/api/scenarios/reset")
    await self._post(f"/api/scenarios/{scenario}/enable", self._parameters[scenario])
    await self._wait_for_runtime_target(scenario)
    trace_ids = await self._send_orders()
    incident_id = await self._start_alert(scenario, trace_ids)
    report = await self._run_until_terminal(incident_id)
    return self._assert_contract(scenario, incident_id, trace_ids, report)
~~~

Use bounded polling for health, runtime config, telemetry visibility, and terminal investigation state. Pass only if root service matches and every report Evidence ID is non-empty and incident-owned; return a stage-specific failure result otherwise.

- [ ] **Step 4: Implement CLI and reset migration**

~~~python
parser.add_argument("--scenario", choices=SCENARIO_NAMES)
parser.add_argument("--all", action="store_true")
~~~

Print scenario, stage, incident ID, root service, cause, and evidence IDs. Replace filesystem/volume reset instructions with POST /api/scenarios/reset. Keep generate_traffic.py reusable and returning request summaries and trace IDs.

- [ ] **Step 5: Verify and commit**

Run: uv run pytest tests/demo/test_runner.py tests/demo/test_run_demo_cli.py -q

Expected: PASS; CLI has nonzero exit code on any failed scenario and all state changes use public APIs.

~~~bash
git add packages/demo scripts tests/demo pyproject.toml README.md
git commit -m "feat: add reusable end-to-end demo runner"
~~~

### Task 5: Five-Scenario Compose Acceptance and Documentation

**Files:**
- Create: tests/integration/conftest.py, tests/integration/test_scenario_acceptance.py
- Modify: tests/integration/test_compose_flow.py, infra/compose/compose.yaml, README.md, docs/evaluation.md
- Test: tests/integration/test_scenario_acceptance.py

**Interfaces:**
- Consumes a healthy Compose environment and DemoRunner.
- Produces one parametrized result for payment_delay, payment_error_rate, db_pool_exhaustion, dependency_unavailable, and deployment_regression.

- [ ] **Step 1: Write the failing Compose acceptance test**

~~~python
@pytest.mark.parametrize(
    ("scenario", "root_service"),
    [
        ("payment_delay", "payment-service"),
        ("payment_error_rate", "payment-service"),
        ("db_pool_exhaustion", "order-service"),
        ("dependency_unavailable", "order-service"),
        ("deployment_regression", "payment-service"),
    ],
)
async def test_scenario_reports_expected_root_service(compose_urls, scenario, root_service) -> None:
    result = await DemoRunner(**compose_urls, traffic_count=5).run(scenario)
    assert result.status == "passed", result.failure_message
    assert result.report["root_service"] == root_service
    assert result.report["evidence_ids"]
~~~

- [ ] **Step 2: Run test to verify failure**

Run: uv run pytest tests/integration/test_scenario_acceptance.py -q

Expected: FAIL until Tasks 1-4 exist. If Docker is unavailable, the fixture reports the missing runtime explicitly rather than silently skipping acceptance.

- [ ] **Step 3: Add Compose lifecycle fixture and telemetry checks**

~~~python
@pytest.fixture(scope="session")
def compose_urls() -> dict[str, str]:
    subprocess.run(COMPOSE_UP, check=True)
    wait_for_health("http://localhost:8000/healthz")
    wait_for_health("http://localhost:8003/healthz")
    yield {"control_plane_url": "http://localhost:8003", "gateway_url": "http://localhost:8000"}
    subprocess.run(COMPOSE_DOWN, check=True)
~~~

For every run, query telemetry through public read-only APIs/tools and assert one cross-service trace, one log, and one metric. Add negative assertions that scenario, investigation, report, and CLI serialization never contain root_cause_label.

- [ ] **Step 4: Document verified workflow**

~~~bash
docker compose -f infra/compose/compose.yaml up --build
uv run python scripts/run_demo.py --all
uv run pytest tests/integration/test_scenario_acceptance.py -q
~~~

Add these commands to README. Update docs/evaluation.md to say five Compose acceptance runs validate the pipeline separately from strategy-comparison metrics.

- [ ] **Step 5: Run final verification and commit**

Run: uv run ruff check . && uv run mypy packages apps && uv run pytest -q && docker compose -f infra/compose/compose.yaml config

Expected: PASS; all five Compose scenarios identify the expected root service and cite current evidence.

~~~bash
git add tests/integration infra/compose README.md docs/evaluation.md
git commit -m "test: verify five end-to-end investigation scenarios"
~~~

## Coverage Review

- Persistent safe scenario control plane: Task 1.
- Runtime configuration and cross-container telemetry: Task 2.
- Current-evidence root-service identification and report guard: Task 3.
- Shared interactive and automated workflow: Task 4.
- Five deterministic Compose acceptance cases, docs, and final quality gate: Task 5.

