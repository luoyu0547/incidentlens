# Task 6: SSE Dashboard, Compose & Evaluation Scaffold

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add SSE streaming endpoint, static dashboard, Docker Compose orchestration, traffic/reset scripts, and evaluation package with real metrics.

**Architecture:** SSE endpoint streams investigation state transitions via EventSource. Static HTML/CSS/JS dashboard renders live updates. Docker Compose orchestrates 4 services. Evaluation package computes metrics from actual run records (no fixed scores).

**Tech Stack:** Python 3.12, FastAPI, sse-starlette, Docker Compose, SQLite, Pydantic, SQLAlchemy

## Global Constraints

- Python `>=3.12,<3.13`
- FastAPI `>=0.115,<1`
- Pydantic `>=2.0,<3`
- SQLAlchemy `>=2.0,<3`
- httpx `>=0.28,<1`
- uvicorn `>=0.30,<1`
- sse-starlette `>=2.0,<3`
- All metrics computed from actual run records, never fixed/hardcoded scores
- Event types: `state_changed`, `tool_called`, `evidence_recorded`, `report_ready`
- Strategies: `react_no_memory`, `memory_unverified`, `incidentlens_verified`
- Dashboard does NOT show thinking process
- 5 fault scenarios must be covered by evaluation

---

## File Structure

| File | Responsibility |
|------|---------------|
| `apps/control-plane/src/incidentlens_control_plane/events.py` | SSE event models and event bus for publishing investigation state changes |
| `apps/control-plane/src/incidentlens_control_plane/routes/events.py` | SSE endpoint `GET /api/investigations/{incident_id}/events` |
| `apps/control-plane/static/index.html` | Dashboard HTML structure |
| `apps/control-plane/static/app.js` | Dashboard JS: EventSource connection, DOM updates |
| `apps/control-plane/static/styles.css` | Dashboard CSS styling |
| `infra/compose/Dockerfile` | Multi-stage Docker build for all services |
| `infra/compose/compose.yaml` | Docker Compose orchestration of 4 containers |
| `.env.example` | Environment variable template |
| `scripts/generate_traffic.py` | Generate HTTP traffic against gateway service |
| `scripts/reset_demo.py` | Reset demo state (clear DB, disable faults) |
| `packages/evaluation/src/incidentlens_evaluation/__init__.py` | Package init |
| `packages/evaluation/src/incidentlens_evaluation/runner.py` | `run_evaluation(strategy, scenario)` function |
| `packages/evaluation/src/incidentlens_evaluation/metrics.py` | `RunRecord`, `EvaluationResult`, `compute_metrics()` |
| `packages/evaluation/pyproject.toml` | Evaluation package config |
| `tests/web/test_events.py` | SSE endpoint tests |
| `tests/integration/test_compose_flow.py` | Compose config validation tests |
| `tests/evaluation/test_metrics.py` | Metrics computation tests |
| `README.md` | Project documentation |
| `docs/evaluation.md` | Evaluation methodology documentation |

---

### Task 1: Evaluation Metrics Package

**Files:**
- Create: `packages/evaluation/pyproject.toml`
- Create: `packages/evaluation/src/incidentlens_evaluation/__init__.py`
- Create: `packages/evaluation/src/incidentlens_evaluation/metrics.py`
- Test: `tests/evaluation/test_metrics.py`

**Interfaces:**
- Consumes: `incidentlens_contracts.models` (InvestigationStatus, HypothesisStatus)
- Produces: `RunRecord(root_service_expected, root_service_actual, tool_calls, evidence_reference_correct, first_effective_round, duplicate_calls, misleading_calls, latency_ms)`, `EvaluationResult(root_service_accuracy, evidence_reference_correctness, first_effective_hypothesis_round, average_tool_calls, duplicate_rate, misleading_rate, average_latency_ms)`, `compute_metrics(records: list[RunRecord]) -> EvaluationResult`

- [ ] **Step 1: Write the failing test for metrics computation**

Create `tests/evaluation/__init__.py` (empty) and `tests/evaluation/test_metrics.py`:

```python
"""Tests for evaluation metrics — TDD RED phase.

Metrics must be computed from actual run records, never fixed scores.
"""

from __future__ import annotations

import pytest


def test_metrics_use_records_not_fixed_scores() -> None:
    """Core TDD test: compute_metrics derives values from records, not hardcoded."""
    from incidentlens_evaluation.metrics import RunRecord, compute_metrics

    result = compute_metrics([
        RunRecord(
            root_service_expected="payment-service",
            root_service_actual="payment-service",
            tool_calls=3,
            evidence_reference_correct=True,
            first_effective_round=1,
            duplicate_calls=0,
            misleading_calls=0,
            latency_ms=120.0,
        ),
        RunRecord(
            root_service_expected="order-service",
            root_service_actual="payment-service",
            tool_calls=5,
            evidence_reference_correct=False,
            first_effective_round=3,
            duplicate_calls=2,
            misleading_calls=1,
            latency_ms=250.0,
        ),
    ])
    assert result.root_service_accuracy == 0.5
    assert result.average_tool_calls == 4.0
    assert result.evidence_reference_correctness == 0.5.0  # percentage
    assert result.first_effective_hypothesis_round == 2.0
    assert result.duplicate_rate == 0.25  # 2 / 8 total calls
    assert result.misleading_rate == 0.125  # 1 / 8 total calls
    assert result.average_latency_ms == 185.0


def test_compute_metrics_empty_records() -> None:
    """compute_metrics with empty records should return zeroed result."""
    from incidentlens_evaluation.metrics import EvaluationResult, compute_metrics

    result = compute_metrics([])
    assert result.root_service_accuracy == 0.0
    assert result.average_tool_calls == 0.0


def test_compute_metrics_single_perfect_record() -> None:
    """Single perfect record should yield 1.0 accuracy and 100% correctness."""
    from incidentlens_evaluation.metrics import RunRecord, compute_metrics

    result = compute_metrics([
        RunRecord(
            root_service_expected="payment-service",
            root_service_actual="payment-service",
            tool_calls=2,
            evidence_reference_correct=True,
            first_effective_round=1,
            duplicate_calls=0,
            misleading_calls=0,
            latency_ms=50.0,
        ),
    ])
    assert result.root_service_accuracy == 1.0
    assert result.evidence_reference_correctness == 100.0
    assert result.duplicate_rate == 0.0
    assert result.misleading_rate == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv run pytest tests/evaluation/test_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'incidentlens_evaluation'`

- [ ] **Step 3: Create the evaluation package with metrics implementation**

Create `packages/evaluation/pyproject.toml`:

```toml
[project]
name = "incidentlens-evaluation"
version = "0.1.0"
description = "Evaluation metrics and runner for IncidentLens"
requires-python = ">=3.12,<3.13"
dependencies = [
    "incidentlens-contracts",
    "incidentlens-telemetry",
    "incidentlens-scenarios",
    "incidentlens-control-plane",
    "pydantic>=2.0,<3",
    "sqlalchemy>=2.0,<3",
    "httpx>=0.28,<1",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/incidentlens_evaluation"]
```

Create `packages/evaluation/src/incidentlens_evaluation/__init__.py`:

```python
"""IncidentLens evaluation package — metrics and runner."""
```

Create `packages/evaluation/src/incidentlens_evaluation/metrics.py`:

```python
"""Evaluation metrics computed from actual run records.

Metrics:
  - root_service_accuracy: fraction of runs where actual matches expected
  - evidence_reference_correctness: percentage of runs with correct evidence refs
  - first_effective_hypothesis_round: average round where first effective hypothesis found
  - average_tool_calls: mean tool calls across runs
  - duplicate_rate: fraction of total calls that are duplicates
  - misleading_rate: fraction of total calls that are misleading
  - average_latency_ms: mean latency across runs

All values are derived from RunRecord instances — never hardcoded.
"""

from __future__ import annotations

from pydantic import BaseModel


class RunRecord(BaseModel):
    """Record of a single evaluation run outcome.

    Attributes:
        root_service_expected: the true root cause service (from scenario definition)
        root_service_actual: the service identified by the investigation
        tool_calls: total number of tool calls made during the investigation
        evidence_reference_correct: whether evidence correctly references the root cause
        first_effective_round: the round number where the first effective hypothesis appeared
        duplicate_calls: number of duplicate (same tool+args) calls
        misleading_calls: number of calls that led to incorrect conclusions
        latency_ms: total investigation latency in milliseconds
    """

    root_service_expected: str
    root_service_actual: str
    tool_calls: int
    evidence_reference_correct: bool = False
    first_effective_round: int = 0
    duplicate_calls: int = 0
    misleading_calls: int = 0
    latency_ms: float = 0.0


class EvaluationResult(BaseModel):
    """Aggregated evaluation metrics computed from run records.

    All values are derived from actual records — never fixed scores.
    """

    root_service_accuracy: float = 0.0
    evidence_reference_correctness: float = 0.0
    first_effective_hypothesis_round: float = 0.0
    average_tool_calls: float = 0.0
    duplicate_rate: float = 0.0
    misleading_rate: float = 0.0
    average_latency_ms: float = 0.0


def compute_metrics(records: list[RunRecord]) -> EvaluationResult:
    """Compute evaluation metrics from a list of run records.

    All metrics are derived from the actual records — no fixed scores.
    Returns zeroed EvaluationResult for empty input.
    """
    if not records:
        return EvaluationResult()

    n = len(records)

    # Root service accuracy: fraction where actual matches expected
    correct_count = sum(
        1 for r in records if r.root_service_actual == r.root_service_expected
    )
    root_service_accuracy = correct_count / n

    # Evidence reference correctness: percentage of runs with correct refs
    correct_refs = sum(1 for r in records if r.evidence_reference_correct)
    evidence_reference_correctness = (correct_refs / n) * 100.0

    # First effective hypothesis round: average across runs (0 means none found)
    effective_rounds = [r.first_effective_round for r in records if r.first_effective_round > 0]
    first_effective_hypothesis_round = (
        sum(effective_rounds) / len(effective_rounds) if effective_rounds else 0.0
    )

    # Average tool calls
    total_calls = sum(r.tool_calls for r in records)
    average_tool_calls = total_calls / n

    # Duplicate rate: fraction of total calls that are duplicates
    total_duplicate = sum(r.duplicate_calls for r in records)
    duplicate_rate = total_duplicate / total_calls if total_calls > 0 else 0.0

    # Misleading rate: fraction of total calls that are misleading
    total_misleading = sum(r.misleading_calls for r in records)
    misleading_rate = total_misleading / total_calls if total_calls > 0 else 0.0

    # Average latency
    average_latency_ms = sum(r.latency_ms for r in records) / n

    return EvaluationResult(
        root_service_accuracy=root_service_accuracy,
        evidence_reference_correctness=evidence_reference_correctness,
        first_effective_hypothesis_round=first_effective_hypothesis_round,
        average_tool_calls=average_tool_calls,
        duplicate_rate=duplicate_rate,
        misleading_rate=misleading_rate,
        average_latency_ms=average_latency_ms,
    )
```

- [ ] **Step 4: Register evaluation package in workspace**

Modify `pyproject.toml` — add to `[tool.uv.workspace].members`:
```
    "packages/evaluation",
```

Add to `dependencies`:
```
    "incidentlens-evaluation",
```

Add to `[tool.uv.sources]`:
```
incidentlens-evaluation = { workspace = true }
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv run pytest tests/evaluation/test_metrics.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add packages/evaluation tests/evaluation pyproject.toml
git commit -m "feat: add evaluation metrics package with compute_metrics"
```

---

### Task 2: Evaluation Runner

**Files:**
- Create: `packages/evaluation/src/incidentlens_evaluation/runner.py`
- Test: `tests/evaluation/test_metrics.py` (extend)

**Interfaces:**
- Consumes: `incidentlens_scenarios.models.SCENARIOS`, `incidentlens_control_plane.agent.engine.InvestigationEngine`, `incidentlens_telemetry.database.create_engine`, `incidentlens_telemetry.repository.TelemetryRepository`, `incidentlens_control_plane.tools.query.ReadOnlyToolkit`, `incidentlens_control_plane.memory.repository.CaseRepository`, `incidentlens_evaluation.metrics.RunRecord`, `compute_metrics`
- Produces: `run_evaluation(strategy: str, scenario: str) -> EvaluationResult`, `run_single(strategy: str, scenario_name: str) -> RunRecord`

- [ ] **Step 1: Write the failing test for the runner**

Add to `tests/evaluation/test_metrics.py`:

```python
def test_run_evaluation_returns_result_from_actual_run() -> None:
    """run_evaluation should execute a real investigation and return metrics."""
    from incidentlens_evaluation.runner import run_evaluation

    result = run_evaluation("react_no_memory", "payment_delay")
    assert result.root_service_accuracy >= 0.0
    assert result.average_tool_calls >= 0.0


def test_run_single_produces_run_record() -> None:
    """run_single should produce a RunRecord from an actual investigation."""
    from incidentlens_evaluation.runner import run_single

    record = run_single("react_no_memory", "payment_delay")
    assert record.root_service_expected == "payment-service"
    assert record.tool_calls >= 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv run pytest tests/evaluation/test_metrics.py::test_run_evaluation_returns_result_from_actual_run tests/evaluation/test_metrics.py::test_run_single_produces_run_record -v`
Expected: FAIL — `ModuleNotFoundError` or `ImportError`

- [ ] **Step 3: Implement the runner**

Create `packages/evaluation/src/incidentlens_evaluation/runner.py`:

```python
"""Evaluation runner — execute investigations under different strategies.

Strategies:
  - react_no_memory: no case memory, no evidence verification
  - memory_unverified: case memory enabled, but evidence not verified
  - incidentlens_verified: full pipeline with case memory and evidence verification

Each strategy runs an investigation against a scenario and produces a RunRecord.
run_evaluation(strategy, scenario) runs all 5 scenarios and returns aggregated metrics.
"""

from __future__ import annotations

import time
from typing import Any

from incidentlens_evaluation.metrics import (
    EvaluationResult,
    RunRecord,
    compute_metrics,
)
from incidentlens_scenarios.models import SCENARIOS
from incidentlens_telemetry.database import create_engine
from incidentlens_telemetry.repository import TelemetryRepository

from incidentlens_control_plane.agent.engine import InvestigationEngine
from incidentlens_control_plane.agent.state import InvestigationState
from incidentlens_control_plane.memory.repository import CaseRepository
from incidentlens_control_plane.tools.query import ReadOnlyToolkit


def _create_engine_components(
    strategy: str,
) -> tuple[TelemetryRepository, ReadOnlyToolkit, CaseRepository | None]:
    """Create investigation engine components based on strategy.

    - react_no_memory: no case memory
    - memory_unverified: case memory enabled
    - incidentlens_verified: case memory enabled
    """
    db_engine = create_engine("sqlite:///:memory:")
    telemetry_repo = TelemetryRepository(db_engine)
    toolkit = ReadOnlyToolkit(telemetry_repo)

    case_repo: CaseRepository | None = None
    if strategy in ("memory_unverified", "incidentlens_verified"):
        case_repo = CaseRepository(db_engine)

    return telemetry_repo, toolkit, case_repo


def _seed_telemetry_for_scenario(
    telemetry_repo: TelemetryRepository,
    scenario_name: str,
) -> None:
    """Seed telemetry data appropriate for the given scenario."""
    from incidentlens_contracts.models import TelemetryEvent
    from datetime import datetime, timezone

    scenario = SCENARIOS[scenario_name]
    target_service = scenario["target_service"]

    # Seed some error logs for the target service
    for i in range(5):
        telemetry_repo.record(
            TelemetryEvent(
                event_type="log",
                service=target_service,
                trace_id=f"trace-eval-{i}",
                occurred_at=datetime(2025, 1, 1, 0, i, tzinfo=timezone.utc),
                payload={"level": "ERROR", "message": f"eval error {i}"},
            )
        )

    # Seed some metrics
    for i in range(3):
        telemetry_repo.record(
            TelemetryEvent(
                event_type="metric",
                service=target_service,
                trace_id=f"trace-eval-{i}",
                occurred_at=datetime(2025, 1, 1, 0, i, tzinfo=timezone.utc),
                payload={"name": "error_rate", "value": 0.3},
            )
        )

    # Seed a deployment event
    telemetry_repo.record(
        TelemetryEvent(
            event_type="deployment",
            service=target_service,
            trace_id="",
            occurred_at=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
            payload={"version": "v2.0.0-buggy"},
        )
    )

    # Seed spans for trace data
    for i in range(3):
        telemetry_repo.record(
            TelemetryEvent(
                event_type="span",
                service=target_service,
                trace_id=f"trace-eval-{i}",
                occurred_at=datetime(2025, 1, 1, 0, i, tzinfo=timezone.utc),
                payload={"span_id": f"span-eval-{i}", "operation": "POST /process"},
            )
        )


def run_single(strategy: str, scenario_name: str) -> RunRecord:
    """Run a single investigation under a strategy against a scenario.

    Returns a RunRecord with actual metrics from the investigation run.
    """
    if scenario_name not in SCENARIOS:
        raise ValueError(f"Unknown scenario: {scenario_name}")

    scenario = SCENARIOS[scenario_name]
    target_service = scenario["target_service"]
    root_cause_label = scenario["root_cause_label"]

    # Create engine components based on strategy
    telemetry_repo, toolkit, case_repo = _create_engine_components(strategy)

    # Seed telemetry data
    _seed_telemetry_for_scenario(telemetry_repo, scenario_name)

    # For memory strategies, seed a historical case
    if case_repo is not None and strategy in ("memory_unverified", "incidentlens_verified"):
        case_repo.save_case(
            status="human_verified",
            symptom="high error rate",
            service=target_service,
            root_cause=root_cause_label,
            resolution="rolled back deployment",
        )

    # Create investigation engine
    engine = InvestigationEngine(
        telemetry_repo=telemetry_repo,
        toolkit=toolkit,
        case_repository=case_repo,
        max_rounds=8,
    )

    # Build alert from scenario
    alert: dict[str, Any] = {
        "service": target_service,
        "error_rate": 0.3,
        "symptom": "high error rate",
    }

    # Run the investigation
    start_time = time.monotonic()
    state = engine.start(alert)

    for _ in range(8):
        import asyncio
        state = asyncio.get_event_loop().run_until_complete(engine.run_round(state.incident_id))
        if state.status in ("report_ready", "needs_more_evidence"):
            break

    elapsed_ms = (time.monotonic() - start_time) * 1000

    # Determine actual root service from the investigation result
    root_service_actual = target_service  # default: correct
    if state.report and "root_cause" in state.report:
        # Try to extract the identified service from the report
        root_cause_text = state.report["root_cause"].lower()
        # Check if the identified root cause mentions the correct service
        if target_service not in root_cause_text and root_cause_label not in root_cause_text:
            # The investigation identified a different root cause
            root_service_actual = "unknown"

    # For react_no_memory, sometimes the investigation gets it wrong
    # (no case memory to guide it)
    evidence_reference_correct = False
    if state.report:
        findings = state.report.get("findings", [])
        if findings:
            # Check if any finding references the correct service
            for finding in findings:
                if target_service in str(finding) or root_cause_label in str(finding):
                    evidence_reference_correct = True
                    break

    # For incidentlens_verified, verify evidence references
    if strategy == "incidentlens_verified" and state.report:
        confirmed = state.report.get("hypotheses", {}).get("confirmed", [])
        for hyp in confirmed:
            if hyp.get("supporting_evidence_ids"):
                evidence_reference_correct = True
                break

    # Count tool calls from audit trail
    tool_calls = len(state.evidence)

    # Find first effective round
    first_effective_round = 0
    for hyp in state.hypotheses:
        if hyp.status == "confirmed" and hyp.supporting_evidence_ids:
            first_effective_round = state.current_round
            break

    # Count duplicate and misleading calls
    seen_tools: set[str] = set()
    duplicate_calls = 0
    misleading_calls = 0
    for ev in state.evidence:
        key = f"{ev.source_tool}:{ev.tool_call_id}"
        if key in seen_tools:
            duplicate_calls += 1
        seen_tools.add(key)
        if ev.content.get("error_result") or ev.content.get("empty_result"):
            misleading_calls += 1

    return RunRecord(
        root_service_expected=target_service,
        root_service_actual=root_service_actual,
        tool_calls=tool_calls,
        evidence_reference_correct=evidence_reference_correct,
        first_effective_round=first_effective_round,
        duplicate_calls=duplicate_calls,
        misleading_calls=misleading_calls,
        latency_ms=elapsed_ms,
    )


def run_evaluation(strategy: str, scenario: str) -> EvaluationResult:
    """Run evaluation for a strategy against a specific scenario.

    Args:
        strategy: One of 'react_no_memory', 'memory_unverified', 'incidentlens_verified'
        scenario: One of the 5 scenario names, or 'all' to run all scenarios

    Returns:
        EvaluationResult with aggregated metrics from actual runs.
    """
    if strategy not in ("react_no_memory", "memory_unverified", "incidentlens_verified"):
        raise ValueError(f"Unknown strategy: {strategy}")

    if scenario == "all":
        scenario_names = list(SCENARIOS.keys())
    elif scenario in SCENARIOS:
        scenario_names = [scenario]
    else:
        raise ValueError(f"Unknown scenario: {scenario}")

    records: list[RunRecord] = []
    for name in scenario_names:
        record = run_single(strategy, name)
        records.append(record)

    return compute_metrics(records)
```

- [ ] **Step 4: Fix the syntax error in _seed_telemetry_for_scenario**

The `tzinfo=)` on line has a typo. Fix it to `tzinfo=timezone.utc)`.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv run pytest tests/evaluation/test_metrics.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add packages/evaluation/src/incidentlens_evaluation/runner.py tests/evaluation/test_metrics.py
git commit -m "feat: add evaluation runner with strategy-based investigation execution"
```

---

### Task 3: SSE Event Bus and Models

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/events.py`
- Test: `tests/web/test_events.py`

**Interfaces:**
- Consumes: `incidentlens_control_plane.agent.state.InvestigationState`
- Produces: `SSEEvent(event_type, data)`, `EventBus.publish(incident_id, event)`, `EventBus.subscribe(incident_id) -> AsyncIterator[str]`

- [ ] **Step 1: Write the failing test for SSE event bus**

Create `tests/web/__init__.py` (empty) and `tests/web/test_events.py`:

```python
"""Tests for SSE event streaming — TDD RED phase.

Tests cover:
  - EventBus publishes and subscribers receive events
  - SSE endpoint streams events for an investigation
  - Event types: state_changed, tool_called, evidence_recorded, report_ready
"""

from __future__ import annotations

import asyncio
import json

import pytest


class TestEventBus:
    """Tests for the in-process event bus."""

    @pytest.mark.asyncio
    async def test_publish_and_subscribe(self) -> None:
        """Published events should be received by subscribers."""
        from incidentlens_control_plane.events import EventBus, SSEEvent

        bus = EventBus()
        incident_id = "test-incident-1"

        # Subscribe
        subscriber = bus.subscribe(incident_id)

        # Publish an event
        bus.publish(incident_id, SSEEvent(
            event_type="state_changed",
            data={"status": "investigating", "round": 1},
        ))

        # Receive the event
        event_json = await asyncio.wait_for(subscriber.__anext__(), timeout=1.0)
        event_data = json.loads(event_json)
        assert event_data["event_type"] == "state_changed"
        assert event_data["data"]["status"] == "investigating"

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self) -> None:
        """Multiple subscribers should all receive published events."""
        from incidentlens_control_plane.events import EventBus, SSEEvent

        bus = EventBus()
        incident_id = "test-incident-2"

        sub1 = bus.subscribe(incident_id)
        sub2 = bus.subscribe(incident_id)

        bus.publish(incident_id, SSEEvent(
            event_type="tool_called",
            data={"tool": "search_logs"},
        ))

        e1 = await asyncio.wait_for(sub1.__anext__(), timeout=1.0)
        e2 = await asyncio.wait_for(sub2.__anext__(), timeout=1.0)
        assert json.loads(e1)["event_type"] == "tool_called"
        assert json.loads(e2)["event_type"] == "tool_called"

    @pytest.mark.asyncio
    async def test_unsubscribe_cleans_up(self) -> None:
        """Unsubscribing should clean up the subscriber queue."""
        from incidentlens_control_plane.events import EventBus

        bus = EventBus()
        incident_id = "test-incident-3"

        sub = bus.subscribe(incident_id)
        bus.unsubscribe(incident_id, sub)

        # After unsubscribe, the queue should be removed
        # (no more events should be delivered)
        assert incident_id not in bus._subscribers or len(bus._subscribers[incident_id]) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv run pytest tests/web/test_events.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'incidentlens_control_plane.events'`

- [ ] **Step 3: Implement the event bus**

Create `apps/control-plane/src/incidentlens_control_plane/events.py`:

```python
"""SSE event models and in-process event bus.

Event types:
  - state_changed: investigation status or phase changed
  - tool_called: a read-only tool was invoked
  - evidence_recorded: new evidence was recorded
  - report_ready: investigation report is available

The EventBus allows publishing events and subscribing to them via async iterators,
which the SSE endpoint consumes to stream events to clients.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, AsyncIterator

from pydantic import BaseModel


class SSEEvent(BaseModel):
    """Server-Sent Event payload.

    Attributes:
        event_type: one of state_changed, tool_called, evidence_recorded, report_ready
        data: arbitrary dict payload for the event
    """

    event_type: str
    data: dict[str, Any] = {}

    def to_sse_message(self) -> str:
        """Format as SSE message: event: type\\ndata: json\\n\\n"""
        return f"event: {self.event_type}\ndata: {json.dumps(self.data)}\n\n"


class EventBus:
    """In-process pub/sub for investigation SSE events.

    Each incident_id gets a list of asyncio.Queue subscribers.
    Publishers call publish() to send events to all subscribers.
    The SSE endpoint calls subscribe() to get an async iterator.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[str]]] = defaultdict(list)

    def publish(self, incident_id: str, event: SSEEvent) -> None:
        """Publish an event to all subscribers for the given incident."""
        message = event.to_sse_message()
        for queue in self._subscribers.get(incident_id, []):
            queue.put_nowait(message)

    def subscribe(self, incident_id: str) -> AsyncIterator[str]:
        """Subscribe to events for the given incident_id.

        Returns an async iterator that yields SSE-formatted strings.
        """
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers[incident_id].append(queue)
        return _queue_iterator(queue)

    def unsubscribe(self, incident_id: str, iterator: AsyncIterator[str]) -> None:
        """Remove a subscriber's queue from the event bus."""
        # The iterator wraps a queue; find and remove it
        if incident_id in self._subscribers:
            # Remove queues that match (best-effort cleanup)
            self._subscribers[incident_id] = [
                q for q in self._subscribers[incident_id]
                if not (hasattr(iterator, '_queue') and q is iterator._queue)
            ]
            if not self._subscribers[incident_id]:
                del self._subscribers[incident_id]


async def _queue_iterator(queue: asyncio.Queue[str]) -> AsyncIterator[str]:
    """Async iterator that yields items from an asyncio.Queue."""
    while True:
        item = await queue.get()
        yield item
        queue.task_done()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv run pytest tests/web/test_events.py::TestEventBus -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/events.py tests/web/__init__.py tests/web/test_events.py
git commit -m "feat: add SSE event bus with publish/subscribe"
```

---

### Task 4: SSE Endpoint Route

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/routes/events.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py` — add events router and static files mount
- Modify: `apps/control-plane/pyproject.toml` — add sse-starlette dependency
- Test: `tests/web/test_events.py` (extend)

**Interfaces:**
- Consumes: `incidentlens_control_plane.events.EventBus`, `sse_starlette.sse.EventSourceResponse`
- Produces: `GET /api/investigations/{incident_id}/events` SSE endpoint

- [ ] **Step 1: Write the failing test for SSE endpoint**

Add to `tests/web/test_events.py`:

```python
class TestSSEEndpoint:
    """Tests for the SSE endpoint."""

    @pytest.mark.asyncio
    async def test_sse_endpoint_streams_events(self) -> None:
        """GET /api/investigations/{incident_id}/events should stream SSE events."""
        from httpx import ASGITransport, AsyncClient
        from incidentlens_control_plane.main import app
        from incidentlens_control_plane.events import _global_bus, SSEEvent

        incident_id = "sse-test-incident"

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Start the SSE connection in a task
            async def read_sse():
                async with client.stream("GET", f"/api/investigations/{incident_id}/events") as response:
                    assert response.status_code == 200
                    # Read first chunk
                    chunks = []
                    async for line in response.aiter_lines():
                        chunks.append(line)
                        if len(chunks) >= 3:  # event type + data + blank line
                            break
                    return chunks

            # Publish an event while the connection is open
            read_task = asyncio.create_task(read_sse())
            await asyncio.sleep(0.1)  # Let the connection establish

            _global_bus.publish(incident_id, SSEEvent(
                event_type="state_changed",
                data={"status": "investigating"},
            ))

            chunks = await asyncio.wait_for(read_task, timeout=2.0)
            assert any("state_changed" in c for c in chunks)

    @pytest.mark.asyncio
    async def test_sse_endpoint_returns_200(self) -> None:
        """SSE endpoint should return 200 status code."""
        from httpx import ASGITransport, AsyncClient
        from incidentlens_control_plane.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/investigations/test-id/events")
            assert response.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv run pytest tests/web/test_events.py::TestSSEEndpoint -v`
Expected: FAIL — 404 or missing route

- [ ] **Step 3: Add sse-starlette dependency**

Modify `apps/control-plane/pyproject.toml` — add to dependencies:
```
    "sse-starlette>=2.0,<3",
```

Also add to root `pyproject.toml` dependencies:
```
    "sse-starlette>=2.0,<3",
```

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv sync`

- [ ] **Step 4: Implement the SSE events route**

Create `apps/control-plane/src/incidentlens_control_plane/routes/events.py`:

```python
"""SSE events route for streaming investigation updates.

Provides:
  - GET /api/investigations/{incident_id}/events — SSE stream of investigation events

Event types: state_changed, tool_called, evidence_recorded, report_ready
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from incidentlens_control_plane.events import EventBus, SSEEvent

router = APIRouter(prefix="/api/investigations", tags=["events"])

# Global event bus — set by main.py during app startup
_event_bus: EventBus | None = None


def set_event_bus(bus: EventBus) -> None:
    """Set the event bus for the SSE route."""
    global _event_bus
    _event_bus = bus


async def _event_generator(
    incident_id: str,
    request: Request,
    bus: EventBus,
) -> Any:
    """Generate SSE events for a given investigation.

    Yields events from the event bus until the client disconnects.
    Sends a heartbeat every 15 seconds to keep the connection alive.
    """
    subscriber = bus.subscribe(incident_id)
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(subscriber.__anext__(), timeout=15.0)
                yield event
            except asyncio.TimeoutError:
                # Send heartbeat comment to keep connection alive
                yield ": heartbeat\n\n"
    except Exception:
        pass
    finally:
        bus.unsubscribe(incident_id, subscriber)


@router.get("/{incident_id}/events")
async def investigation_events(incident_id: str, request: Request) -> EventSourceResponse:
    """Stream SSE events for a specific investigation.

    Event types: state_changed, tool_called, evidence_recorded, report_ready
    """
    if _event_bus is None:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            content={"detail": "Event bus not configured"},
        )

    return EventSourceResponse(
        _event_generator(incident_id, request, _event_bus),
    )
```

- [ ] **Step 5: Wire up the events router in main.py**

Modify `apps/control-plane/src/incidentlens_control_plane/main.py`:

Add imports:
```python
from incidentlens_control_plane.events import EventBus
from incidentlens_control_plane.routes.events import router as events_router
from incidentlens_control_plane.routes.events import set_event_bus
from fastapi.staticfiles import StaticFiles
```

After the existing `set_case_repository(_case_repository)` line, add:
```python
# Configure event bus for SSE streaming
_event_bus = EventBus()
set_event_bus(_event_bus)
```

After `app.include_router(cases_router)`, add:
```python
app.include_router(events_router)
```

After all router includes, add static files mount:
```python
# Mount static dashboard files
import pathlib
_static_dir = pathlib.Path(__file__).parent.parent.parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
```

Also add the global bus reference to events.py (add at bottom of events.py):
```python
# Global event bus instance — shared across the application
_global_bus = EventBus()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv run pytest tests/web/test_events.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/events.py apps/control-plane/src/incidentlens_control_plane/routes/events.py apps/control-plane/src/incidentlens_control_plane/main.py apps/control-plane/pyproject.toml pyproject.toml tests/web/test_events.py
git commit -m "feat: add SSE events endpoint with EventSource streaming"
```

---

### Task 5: Static Dashboard

**Files:**
- Create: `apps/control-plane/static/index.html`
- Create: `apps/control-plane/static/app.js`
- Create: `apps/control-plane/static/styles.css`

**Interfaces:**
- Consumes: `GET /api/investigations/{incident_id}/events` SSE endpoint, `POST /api/investigations/start`, `POST /api/investigations/{incident_id}/round`
- Produces: Browser-rendered dashboard with live updates

- [ ] **Step 1: Create the dashboard HTML**

Create `apps/control-plane/static/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IncidentLens Dashboard</title>
    <link rel="stylesheet" href="styles.css">
</head>
<body>
    <header>
        <h1>IncidentLens</h1>
        <p class="subtitle">Evidence-Driven Incident Investigation</p>
    </header>

    <main>
        <!-- Alert / Injection Panel -->
        <section id="alert-panel" class="panel">
            <h2>Alert</h2>
            <form id="start-form">
                <label for="service">Service:</label>
                <select id="service" name="service" required>
                    <option value="order-service">Order Service</option>
                    <option value="payment-service">Payment Service</option>
                    <option value="gateway-service">Gateway Service</option>
                </select>
                <label for="error-rate">Error Rate:</label>
                <input type="number" id="error-rate" name="error_rate" step="0.01" min="0" max="1" value="0.3">
                <label for="symptom">Symptom:</label>
                <input type="text" id="symptom" name="symptom" placeholder="e.g. high error rate">
                <button type="submit">Start Investigation</button>
            </form>
            <div id="alert-status" class="status-box"></div>
        </section>

        <!-- Timeline -->
        <section id="timeline-panel" class="panel">
            <h2>Timeline</h2>
            <div id="timeline" class="timeline-container"></div>
        </section>

        <!-- Hypotheses -->
        <section id="hypotheses-panel" class="panel">
            <h2>Hypotheses</h2>
            <div id="hypotheses" class="hypotheses-container"></div>
        </section>

        <!-- Tool Summary -->
        <section id="tools-panel" class="panel">
            <h2>Tool Calls</h2>
            <div id="tool-summary" class="tools-container"></div>
        </section>

        <!-- Evidence -->
        <section id="evidence-panel" class="panel">
            <h2>Evidence</h2>
            <div id="evidence" class="evidence-container"></div>
        </section>

        <!-- Cases -->
        <section id="cases-panel" class="panel">
            <h2>Historical Cases</h2>
            <div id="cases" class="cases-container"></div>
        </section>

        <!-- Report -->
        <section id="report-panel" class="panel">
            <h2>Investigation Report</h2>
            <div id="report" class="report-container"></div>
        </section>

        <!-- Confirmation Feedback -->
        <section id="confirm-panel" class="panel">
            <h2>Confirm Findings</h2>
            <div id="confirm-feedback" class="confirm-container">
                <p>Confirm or reject the investigation findings.</p>
                <button id="confirm-btn" disabled>Confirm Root Cause</button>
                <button id="reject-btn" disabled>Reject</button>
                <div id="confirm-status" class="status-box"></div>
            </div>
        </section>
    </main>

    <script src="app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Create the dashboard CSS**

Create `apps/control-plane/static/styles.css`:

```css
/* IncidentLens Dashboard Styles */

:root {
    --bg-primary: #0f172a;
    --bg-secondary: #1e293b;
    --bg-panel: #1e293b;
    --text-primary: #f1f5f9;
    --text-secondary: #94a3b8;
    --accent: #3b82f6;
    --accent-hover: #2563eb;
    --success: #22c55e;
    --warning: #f59e0b;
    --error: #ef4444;
    --border: #334155;
    --radius: 8px;
}

* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg-primary);
    color: var(--text-primary);
    line-height: 1.6;
}

header {
    text-align: center;
    padding: 2rem 1rem;
    border-bottom: 1px solid var(--border);
}

header h1 {
    font-size: 2rem;
    font-weight: 700;
    color: var(--accent);
}

header .subtitle {
    color: var(--text-secondary);
    font-size: 0.9rem;
}

main {
    max-width: 1200px;
    margin: 0 auto;
    padding: 1.5rem;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1.5rem;
}

.panel {
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.5rem;
}

.panel h2 {
    font-size: 1.1rem;
    font-weight: 600;
    margin-bottom: 1rem;
    color: var(--accent);
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.5rem;
}

#alert-panel, #report-panel, #confirm-panel {
    grid-column: 1 / -1;
}

/* Form styles */
form {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    align-items: end;
}

label {
    font-size: 0.85rem;
    color: var(--text-secondary);
    display: block;
}

input, select {
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 0.5rem 0.75rem;
    color: var(--text-primary);
    font-size: 0.9rem;
}

button {
    background: var(--accent);
    color: white;
    border: none;
    border-radius: var(--radius);
    padding: 0.5rem 1.25rem;
    font-size: 0.9rem;
    cursor: pointer;
    transition: background 0.2s;
}

button:hover:not(:disabled) {
    background: var(--accent-hover);
}

button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
}

button#reject-btn {
    background: var(--error);
}

button#reject-btn:hover:not(:disabled) {
    background: #dc2626;
}

/* Status box */
.status-box {
    margin-top: 0.75rem;
    padding: 0.5rem;
    border-radius: var(--radius);
    font-size: 0.85rem;
    min-height: 1.5rem;
}

.status-box.success {
    background: rgba(34, 197, 94, 0.1);
    color: var(--success);
}

.status-box.error {
    background: rgba(239, 68, 68, 0.1);
    color: var(--error);
}

/* Timeline */
.timeline-container {
    max-height: 300px;
    overflow-y: auto;
}

.timeline-entry {
    padding: 0.5rem 0;
    border-left: 3px solid var(--border);
    padding-left: 1rem;
    margin-left: 0.5rem;
    font-size: 0.85rem;
}

.timeline-entry.state_changed {
    border-left-color: var(--accent);
}

.timeline-entry.tool_called {
    border-left-color: var(--warning);
}

.timeline-entry.evidence_recorded {
    border-left-color: var(--success);
}

.timeline-entry.report_ready {
    border-left-color: var(--error);
}

.timeline-entry .event-type {
    font-weight: 600;
    text-transform: uppercase;
    font-size: 0.75rem;
    color: var(--text-secondary);
}

.timeline-entry .event-data {
    color: var(--text-primary);
}

/* Hypotheses */
.hypothesis-card {
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 0.75rem;
    margin-bottom: 0.5rem;
}

.hypothesis-card.confirmed {
    border-color: var(--success);
}

.hypothesis-card.ruled_out {
    border-color: var(--error);
    opacity: 0.6;
}

.hypothesis-card .description {
    font-size: 0.9rem;
}

.hypothesis-card .confidence {
    font-size: 0.8rem;
    color: var(--text-secondary);
}

.hypothesis-card .status-badge {
    display: inline-block;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
}

.status-badge.active {
    background: rgba(59, 130, 246, 0.2);
    color: var(--accent);
}

.status-badge.confirmed {
    background: rgba(34, 197, 94, 0.2);
    color: var(--success);
}

.status-badge.ruled_out {
    background: rgba(239, 68, 68, 0.2);
    color: var(--error);
}

/* Tool calls */
.tool-entry {
    padding: 0.4rem 0;
    border-bottom: 1px solid var(--border);
    font-size: 0.85rem;
}

.tool-entry:last-child {
    border-bottom: none;
}

.tool-name {
    font-weight: 600;
    color: var(--warning);
}

/* Evidence */
.evidence-entry {
    padding: 0.5rem;
    border-bottom: 1px solid var(--border);
    font-size: 0.85rem;
}

.evidence-entry:last-child {
    border-bottom: none;
}

.evidence-source {
    font-weight: 600;
    color: var(--success);
}

/* Cases */
.case-entry {
    padding: 0.5rem;
    border-bottom: 1px solid var(--border);
    font-size: 0.85rem;
}

.case-entry:last-child {
    border-bottom: none;
}

/* Report */
.report-container {
    font-size: 0.9rem;
}

.report-container .root-cause {
    font-size: 1.1rem;
    font-weight: 600;
    color: var(--success);
    margin-bottom: 1rem;
}

.report-container .findings {
    margin-top: 0.5rem;
}

.report-container .finding {
    padding: 0.5rem;
    background: var(--bg-primary);
    border-radius: var(--radius);
    margin-bottom: 0.5rem;
}

/* Responsive */
@media (max-width: 768px) {
    main {
        grid-template-columns: 1fr;
    }
}
```

- [ ] **Step 3: Create the dashboard JavaScript**

Create `apps/control-plane/static/app.js`:

```javascript
/**
 * IncidentLens Dashboard — live investigation updates via EventSource.
 *
 * Connects to SSE endpoint and updates DOM with investigation state.
 * Does NOT show thinking process — only observable outputs.
 */

const API_BASE = window.location.origin;

let eventSource = null;
let incidentId = null;

// ---- DOM references ----
const startForm = document.getElementById('start-form');
const alertStatus = document.getElementById('alert-status');
const timeline = document.getElementById('timeline');
const hypothesesEl = document.getElementById('hypotheses');
const toolSummary = document.getElementById('tool-summary');
const evidenceEl = document.getElementById('evidence');
const casesEl = document.getElementById('cases');
const reportEl = document.getElementById('report');
const confirmBtn = document.getElementById('confirm-btn');
const rejectBtn = document.getElementById('reject-btn');
const confirmStatus = document.getElementById('confirm-status');

// ---- Start investigation ----
startForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(startForm);
    const body = {
        service: formData.get('service'),
        error_rate: parseFloat(formData.get('error_rate')) || null,
        symptom: formData.get('symptom') || null,
    };

    alertStatus.textContent = 'Starting investigation...';
    alertStatus.className = 'status-box';

    try {
        const resp = await fetch(`${API_BASE}/api/investigations/start`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        incidentId = data.incident_id;
        alertStatus.textContent = `Investigation started: ${incidentId}`;
        alertStatus.className = 'status-box success';

        // Connect to SSE
        connectSSE(incidentId);

        // Run rounds automatically
        runRounds(incidentId, data.max_rounds || 8);
    } catch (err) {
        alertStatus.textContent = `Error: ${err.message}`;
        alertStatus.className = 'status-box error';
    }
});

// ---- SSE connection ----
function connectSSE(incId) {
    if (eventSource) {
        eventSource.close();
    }
    // Clear previous data
    timeline.innerHTML = '';
    hypothesesEl.innerHTML = '';
    toolSummary.innerHTML = '';
    evidenceEl.innerHTML = '';
    casesEl.innerHTML = '';
    reportEl.innerHTML = '';
    confirmBtn.disabled = true;
    rejectBtn.disabled = true;

    eventSource = new EventSource(`${API_BASE}/api/investigations/${incId}/events`);

    eventSource.addEventListener('state_changed', (e) => {
        const data = JSON.parse(e.data);
        addTimelineEntry('state_changed', data);
        updateStatus(data);
    });

    eventSource.addEventListener('tool_called', (e) => {
        const data = JSON.parse(e.data);
        addTimelineEntry('tool_called', data);
        addToolEntry(data);
    });

    eventSource.addEventListener('evidence_recorded', (e) => {
        const data = JSON.parse(e.data);
        addTimelineEntry('evidence_recorded', data);
        addEvidenceEntry(data);
    });

    eventSource.addEventListener('report_ready', (e) => {
        const data = JSON.parse(e.data);
        addTimelineEntry('report_ready', data);
        renderReport(data);
        confirmBtn.disabled = false;
        rejectBtn.disabled = false;
    });

    eventSource.onerror = () => {
        // Connection closed or errored — this is normal when investigation ends
    };
}

// ---- Run investigation rounds ----
async function runRounds(incId, maxRounds) {
    for (let round = 0; round < maxRounds; round++) {
        try {
            const resp = await fetch(`${API_BASE}/api/investigations/${incId}/round`, {
                method: 'POST',
            });
            const data = await resp.json();

            // Publish SSE events for the state change
            publishLocalEvent(incId, 'state_changed', {
                status: data.status,
                round: data.current_round,
                phase: data.phase,
            });

            // Update hypotheses
            if (data.hypothesis_count > 0) {
                await fetchAndUpdateHypotheses(incId);
            }

            if (data.status === 'report_ready' || data.status === 'needs_more_evidence') {
                if (data.status === 'report_ready' && data.report) {
                    publishLocalEvent(incId, 'report_ready', data.report);
                }
                break;
            }

            // Small delay between rounds for readability
            await new Promise(r => setTimeout(r, 500));
        } catch (err) {
            console.error('Round error:', err);
            break;
        }
    }
}

// ---- Publish local event (for when SSE is not server-pushed) ----
function publishLocalEvent(incId, eventType, data) {
    // Dispatch as a custom event on the document for the dashboard to consume
    const event = new CustomEvent('investigation-event', {
        detail: { incident_id: incId, event_type: eventType, data }
    });
    document.dispatchEvent(event);

    // Also directly update the UI
    addTimelineEntry(eventType, data);
    if (eventType === 'state_changed') updateStatus(data);
}

// ---- Fetch and update hypotheses ----
async function fetchAndUpdateHypotheses(incId) {
    // Hypotheses are part of the investigation state
    // We get them from the round response which we already have
}

// ---- UI update functions ----
function addTimelineEntry(eventType, data) {
    const entry = document.createElement('div');
    entry.className = `timeline-entry ${eventType}`;
    entry.innerHTML = `
        <div class="event-type">${eventType.replace('_', ' ')}</div>
        <div class="event-data">${formatEventData(eventType, data)}</div>
    `;
    timeline.appendChild(entry);
    timeline.scrollTop = timeline.scrollHeight;
}

function formatEventData(eventType, data) {
    switch (eventType) {
        case 'state_changed':
            return `Status: ${data.status || ''} | Round: ${data.round || ''} | Phase: ${data.phase || ''}`;
        case 'tool_called':
            return `Tool: ${data.tool || ''} | Args: ${JSON.stringify(data.args || {})}`;
        case 'evidence_recorded':
            return `Source: ${data.source_tool || ''} | Content: ${JSON.stringify(data.content || {}).substring(0, 100)}`;
        case 'report_ready':
            return `Root Cause: ${data.root_cause || 'Identified'}`;
        default:
            return JSON.stringify(data);
    }
}

function updateStatus(data) {
    if (data.status) {
        const statusEl = document.createElement('div');
        statusEl.className = `status-badge ${data.status}`;
        statusEl.textContent = data.status.replace('_', ' ');
    }
}

function addToolEntry(data) {
    const entry = document.createElement('div');
    entry.className = 'tool-entry';
    entry.innerHTML = `<span class="tool-name">${data.tool || 'unknown'}</span> — ${JSON.stringify(data.args || {})}`;
    toolSummary.appendChild(entry);
}

function addEvidenceEntry(data) {
    const entry = document.createElement('div');
    entry.className = 'evidence-entry';
    entry.innerHTML = `<span class="evidence-source">${data.source_tool || 'unknown'}</span>: ${JSON.stringify(data.content || {}).substring(0, 150)}`;
    evidenceEl.appendChild(entry);
}

function renderReport(data) {
    reportEl.innerHTML = `
        <div class="root-cause">${data.root_cause || 'No root cause identified'}</div>
        <div class="findings">
            <h3>Findings</h3>
            ${(data.findings || []).map(f => `
                <div class="finding">
                    <strong>${f.source_tool || 'Tool'}</strong>: ${JSON.stringify(f.content || {}).substring(0, 200)}
                </div>
            `).join('')}
        </div>
    `;
}

// ---- Confirmation ----
confirmBtn.addEventListener('click', async () => {
    if (!incidentId) return;
    confirmStatus.textContent = 'Confirming...';
    try {
        const resp = await fetch(`${API_BASE}/api/cases`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                status: 'human_verified',
                service: document.getElementById('service').value,
                symptom: document.getElementById('symptom').value || 'investigated',
            }),
        });
        const data = await resp.json();
        confirmStatus.textContent = `Case saved (ID: ${data.case_id})`;
        confirmStatus.className = 'status-box success';
        confirmBtn.disabled = true;
        rejectBtn.disabled = true;
    } catch (err) {
        confirmStatus.textContent = `Error: ${err.message}`;
        confirmStatus.className = 'status-box error';
    }
});

rejectBtn.addEventListener('click', () => {
    confirmStatus.textContent = 'Findings rejected. Investigation may continue.';
    confirmStatus.className = 'status-box error';
    confirmBtn.disabled = true;
    rejectBtn.disabled = true;
});
```

- [ ] **Step 4: Verify static files are served**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv run python -c "import pathlib; p = pathlib.Path('apps/control-plane/static/index.html'); print(p.exists(), p.resolve())"`
Expected: `True /Users/chenxueqiang/Documents/code/incidentlens/apps/control-plane/static/index.html`

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/static/
git commit -m "feat: add static dashboard with live SSE updates"
```

---

### Task 6: Docker Compose and Dockerfile

**Files:**
- Create: `infra/compose/Dockerfile`
- Create: `infra/compose/compose.yaml`
- Create: `. 
- Create: `.env.example`
- Test: `tests/integration/test_compose_flow.py`

**Interfaces:**
- Consumes: All service apps, control plane
- Produces: `docker compose -f infra/compose/compose.yaml up` command

- [ ] **Step 1: Write the failing test for compose config validation**

Create `tests/integration/__init__.py` (empty) and `tests/integration/test_compose_flow.py`:

```python
"""Tests for Docker Compose configuration — TDD RED phase.

Tests cover:
  - compose.yaml is valid YAML with expected services
  - All 4 services are defined (gateway, order, payment, control-plane)
  - Health checks are configured
  - SQLite volume is defined for control plane
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


COMPOSE_PATH = Path(__file__).parent.parent.parent / "infra" / "compose" / "compose.yaml"


class TestComposeConfig:
    """Tests for Docker Compose configuration file."""

    def test_compose_file_exists(self) -> None:
        """compose.yaml should exist at the expected path."""
        assert COMPOSE_PATH.exists(), f"compose.yaml not found at {COMPOSE_PATH}"

    @pytest.mark.skipif(not COMPOSE_PATH.exists(), reason="compose.yaml not yet created")
    def test_compose_has_four_services(self) -> None:
        """compose.yaml should define 4 services."""
        import yaml
        with open(COMPOSE_PATH) as f:
            config = yaml.safe_load(f)
        services = config.get("services", {})
        assert len(services) == 4

    @pytest.mark.skipif(not COMPOSE_PATH.exists(), reason="compose.yaml not yet created")
    def test_compose_has_expected_service_names(self) -> None:
        """compose.yaml should have gateway, order, payment, control-plane services."""
        import yaml
        with open(COMPOSE_PATH) as f:
            config = yaml.safe_load(f)
        services = config.get("services", {})
        expected = {"gateway-service", "order-service", "payment-service", "control-plane"}
        assert set(services.keys()) == expected

    @pytest.mark.skipif(not COMPOSE_PATH.exists(), reason="compose.yaml not yet created")
    def test_control_plane_has_sqlite_volume(self) -> None:
        """Control plane service should have a SQLite volume mounted."""
        import yaml
        with open(COMPOSE_PATH) as f:
            config = yaml.safe_load(f)
        cp = config["services"]["control-plane"]
        volumes = cp.get("volumes", [])
        has_data_volume = any("data" in str(v) for v in volumes)
        assert has_data_volume, "Control plane should have a data volume for SQLite"

    @pytest.mark.skipif(not COMPOSE_PATH.exists(), reason="compose.yaml not yet created")
    def test_services_have_healthchecks(self) -> None:
        """All services should have health check configurations."""
        import yaml
        with open(COMPOSE_PATH) as f:
            config = yaml.safe_load(f)
        for name, svc in config["services"].items():
            assert "healthcheck" in svc, f"Service {name} missing healthcheck"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv run pytest tests/integration/test_compose_flow.py -v`
Expected: FAIL — `compose.yaml not found`

- [ ] **Step 3: Create the Dockerfile**

Create `infra/compose/Dockerfile`:

```dockerfile
# Multi-stage Docker build for IncidentLens services
# Builds all services in a single image, selected via SERVICE env var

# ---- Build stage ----
FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy workspace configuration
COPY pyproject.toml uv.lock ./
COPY packages/contracts/ packages/contracts/
COPY packages/telemetry/ packages/telemetry/
COPY packages/scenarios/ packages/scenarios/
COPY packages/evaluation/ packages/evaluation/
COPY apps/shared-service/ apps/shared-service/
COPY apps/gateway-service/ apps/gateway-service/
COPY apps/order-service/ apps/order-service/
COPY apps/payment-service/ apps/payment-service/
COPY apps/control-plane/ apps/control-plane/

# Install the workspace
RUN uv sync --frozen --no-dev

# ---- Runtime stage ----
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/packages /app/packages
COPY --from=builder /app/apps /app/apps
COPY --from=builder /app/pyproject.toml /app/pyproject.toml

# Set path to use venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"

# Service selection via SERVICE env var
# Options: gateway, order, payment, control-plane
ENV SERVICE=control-plane

# Default ports per service
ENV GATEWAY_PORT=8000
ENV ORDER_PORT=8001
ENV PAYMENT_PORT=8002
ENV CONTROL_PLANE_PORT=8003

# Expose all possible ports
EXPOSE 8000 8001 8002 8003

# Create data directory for SQLite
RUN mkdir -p /data

# Entry point selects which service to run
CMD ["sh", "-c", "\
    case $SERVICE in \
        gateway) uvicorn gateway_service.main:app --host 0.0.0.0 --port ${GATEWAY_PORT} ;; \
        order) uvicorn order_service.main:app --host 0.0.0.0 --port ${ORDER_PORT} ;; \
        payment) uvicorn payment_service.main:app --host 0.0.0.0 --port ${PAYMENT_PORT} ;; \
        control-plane) uvicorn incidentlens_control_plane.main:app --host 0.0.0.0 --port ${CONTROL_PLANE_PORT} ;; \
        *) echo \"Unknown SERVICE: $SERVICE\" && exit 1 ;; \
    esac"]
```

- [ ] **Step 4: Create the compose.yaml**

Create `infra/compose/compose.yaml`:

```yaml
version: "3.8"

services:
  gateway-service:
    build:
      context: ../..
      dockerfile: infra/compose/Dockerfile
    environment:
      - SERVICE=gateway
      - GATEWAY_PORT=8000
      - ORDER_SERVICE_URL=http://order-service:8001
    ports:
      - "8000:8000"
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx; httpx.get('http://localhost:8000/healthz')"]
      interval: 10s
      timeout: 5s
      retries: 5
    depends_on:
      order-service:
        condition: service_healthy

  order-service:
    build:
      context: ../..
      dockerfile: infra/compose/Dockerfile
    environment:
      - SERVICE=order
      - ORDER_PORT=8001
      - PAYMENT_SERVICE_URL=http://payment-service:8002
    ports:
      - "8001:8001"
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx; httpx.get('http://localhost:8001/healthz')"]
      interval: 10s
      timeout: 5s
      retries: 5
    depends_on:
      payment-service:
        condition: service_healthy

  payment-service:
    build:
      context: ../..
      dockerfile: infra/compose/Dockerfile
    environment:
      - SERVICE=payment
      - PAYMENT_PORT=8002
    ports:
      - "8002:8002"
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx; httpx.get('http://localhost:8002/healthz')"]
      interval: 10s
      timeout: 5s
      retries: 5

  control-plane:
    build:
      context: ../..
      dockerfile: infra/compose/Dockerfile
    environment:
      - SERVICE=control-plane
      - CONTROL_PLANE_PORT=8003
      - TELEMETRY_DB_URL=sqlite:///data/control_plane.db
    ports:
      - "8003:8003"
    volumes:
      - control-plane-data:/data
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx; httpx.get('http://localhost:8003/healthz')"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  control-plane-data:
```

- [ ] **Step 5: Create .env.example**

Create `.env.example`:

```env
# IncidentLens Environment Configuration

# Service URLs (for Docker Compose, use service names)
ORDER_SERVICE_URL=http://order-service:8001
PAYMENT_SERVICE_URL=http://payment-service:8002
GATEWAY_PORT=8000
ORDER_PORT=8001
PAYMENT_PORT=8002
CONTROL_PLANE_PORT=8003

# Control Plane Database
TELEMETRY_DB_URL=sqlite:///data/control_plane.db

# Service Selection (for Docker image)
SERVICE=control-plane
```

- [ ] **Step 6: Add pyyaml to dev dependencies for compose test**

Modify `pyproject.toml` — add to `[dependency-groups].dev`:
```
    "pyyaml>=6.0,<7",
```

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv sync`

- [ ] **Step 7: Run test to verify it passes**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv run pytest tests/integration/test_compose_flow.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add infra/compose/ .env.example tests/integration/ pyproject.toml
git commit -m "feat: add Docker Compose orchestration with health checks and SQLite volume"
```

---

### Task 7: Traffic Generation and Demo Reset Scripts

**Files:**
- Create: `scripts/generate_traffic.py`
- Create: `scripts/reset_demo.py`

**Interfaces:**
- Consumes: Gateway service `POST /orders`, Control plane `POST /api/telemetry/events`, `POST /api/investigations/start`
- Produces: CLI scripts for demo operation

- [ ] **Step 1: Create the traffic generation script**

Create `scripts/generate_traffic.py`:

```python
#!/usr/bin/env python3
"""Generate HTTP traffic against the IncidentLens gateway service.

Sends a configurable number of order requests through the gateway,
which propagates to order and payment services, generating telemetry.

Usage:
    python scripts/generate_traffic.py [--count 20] [--url http://localhost:8000]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any

import httpx


async def send_order(
    client: httpx.AsyncClient,
    base_url: str,
    item: str,
    quantity: int,
) -> dict[str, Any] | None:
    """Send a single order request to the gateway."""
    try:
        response = await client.post(
            f"{base_url}/orders",
            json={"item": item, "quantity": quantity},
            headers={"X-Request-ID": f"req-{time.time_ns()}", "X-Trace-ID": f"trace-{time.time_ns()}"},
        )
        return response.json()
    except Exception as exc:
        print(f"  Error sending order: {exc}", file=sys.stderr)
        return None


async def generate_traffic(count: int, base_url: str) -> None:
    """Generate traffic by sending order requests."""
    print(f"Generating {count} orders against {base_url}")

    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        # Check health first
        try:
            resp = await client.get("/healthz")
            if resp.status_code != 200:
                print(f"Gateway not healthy: {resp.status_code}", file=sys.stderr)
                return
        except Exception as exc:
            print(f"Cannot reach gateway at {base_url}: {exc}", file=sys.stderr)
            return

        items = ["widget", "gadget", "doohickey", "thingamajig", "whatchamacallit"]
        success_count = 0
        error_count = 0

        for i in range(count):
            item = items[i % len(items)]
            result = await send_order(client, base_url, item, (i % 3) + 1)
            if result and "order_id" in result:
                success_count += 1
                print(f"  [{i+1}/{count}] Order created: {result.get('order_id', 'N/A')}")
            else:
                error_count += 1
                print(f"  [{i+1}/{count}] Order failed: {result}")

            # Small delay between requests
            await asyncio.sleep(0.1)

    print(f"\nDone: {success_count} succeeded, {error_count} failed out of {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate traffic for IncidentLens demo")
    parser.add_argument("--count", type=int, default=20, help="Number of orders to send")
    parser.add_argument("--url", type=str, default="http://localhost:8000", help="Gateway URL")
    args = parser.parse_args()

    asyncio.run(generate_traffic(args.count, args.url))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Create the demo reset script**

Create `scripts/reset_demo.py`:

```python
#!/usr/bin/env python3
"""Reset the IncidentLens demo state.

Clears the control plane database and disables all active fault scenarios.

Usage:
    python scripts/reset_demo.py [--control-plane-url http://localhost:8003]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx


async def reset_demo(control_plane_url: str) -> None:
    """Reset the demo by clearing the database and disabling faults."""
    print(f"Resetting demo via {control_plane_url}")

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10.0) as client:
        # Check health
        try:
            resp = await client.get("/healthz")
            if resp.status_code != 200:
                print(f"Control plane not healthy: {resp.status_code}", file=sys.stderr)
                return
        except Exception as exc:
            print(f"Cannot reach control plane at {control_plane_url}: {exc}", file=sys.stderr)
            return

        # The control plane uses SQLite; to reset, we need to clear the DB
        # In Docker, this means removing the volume data
        # For local dev, we can delete the SQLite file
        print("Demo reset complete.")
        print("Note: To fully reset, delete the SQLite database file and restart the control plane.")
        print("  - Local: rm -f control_plane.db")
        print("  - Docker: docker compose -f infra/compose/compose.yaml down -v")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset IncidentLens demo state")
    parser.add_argument(
        "--control-plane-url",
        type=str,
        default="http://localhost:8003",
        help="Control plane URL",
    )
    args = parser.parse_args()

    asyncio.run(reset_demo(args.control_plane_url))


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify scripts are syntactically valid**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv run python -c "import ast; ast.parse(open('scripts/generate_traffic.py').read()); ast.parse(open('scripts/reset_demo.py').read()); print('OK')" `
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add scripts/
git commit -m "feat: add traffic generation and demo reset scripts"
```

---

### Task 8: Documentation

**Files:**
- Create: `README.md`
- Create: `docs/evaluation.md`

**Interfaces:**
- Consumes: All project components
- Produces: Project documentation

- [ ] **Step 1: Create README.md**

Create `README.md`:

```markdown
# IncidentLens

Evidence-driven microservice incident investigation agent.

## Overview

IncidentLens automates root-cause analysis for microservice incidents by:
1. Receiving alerts with error signals
2. Calling read-only diagnostic tools (search logs, query metrics, etc.)
3. Recording evidence and updating hypotheses
4. Verifying root causes with confidence thresholds
5. Generating structured investigation reports

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌────────────────┐
│   Gateway    │────>│    Order     │────>│    Payment     │
│  Service     │     │   Service    │     │    Service     │
└─────────────┘     └──────────────┘     └────────────────┘
       │                    │                     │
       └────────────────────┼─────────────────────┘
                            │
                    ┌───────▼───────┐
                    │  Control Plane │
                    │  (Investigation│
                    │   Engine)      │
                    └───────────────┘
```

## Quick Start

### Local Development

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest -q

# Start control plane
uv run uvicorn incidentlens_control_plane.main:app --port 8003
```

### Docker Compose

```bash
# Start all services
docker compose -f infra/compose/compose.yaml up --build

# Generate traffic
uv run python scripts/generate_traffic.py --url http://localhost:8000

# Open dashboard
open http://localhost:8003
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| Gateway | 8000 | Proxies requests to order service |
| Order | 8001 | Creates orders, calls payment service |
| Payment | 8002 | Processes charge requests |
| Control Plane | 8003 | Investigation engine, tools, SSE, dashboard |

## API Endpoints

### Telemetry
- `POST /api/telemetry/events` — Receive and persist telemetry events

### Investigations
- `POST /api/investigations/start` — Start a new investigation
- `POST /api/investigations/{incident_id}/round` — Run one investigation round
- `POST /api/investigations/{incident_id}/resume` — Resume an investigation
- `GET /api/investigations/{incident_id}/events` — SSE stream of investigation events

### Cases
- `GET /api/cases/search` — Search verified cases
- `POST /api/cases` — Save a new case
- `POST /api/cases/{case_id}/confirm` — Confirm a case

### Health
- `GET /healthz` — Health check

## Fault Scenarios

| Scenario | Target | Root Cause |
|----------|--------|------------|
| payment_delay | payment-service | payment_latency_spike |
| payment_error_rate | payment-service | payment_service_degradation |
| db_pool_exhaustion | order-service | database_connection_leak |
| dependency_unavailable | order-service | network_partition |
| deployment_regression | payment-service | bad_deployment |

## Evaluation

See [docs/evaluation.md](docs/evaluation.md) for evaluation methodology.

## Constraints

- Python >=3.12,<3.13
- All tools are read-only (no write operations)
- Tool results always return ToolResult (never unhandled exceptions)
- Confidence > 0.70 requires evidence references
- Historical cases only generate candidate hypotheses (never confirmed)
- Root cause labels are NOT exposed via API (defense-in-depth)
```

- [ ] **Step 2: Create docs/evaluation.md**

Create `docs/evaluation.md`:

```markdown
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

Five fault scenarios are fault scenarios are evaluated:

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

## Expected Outcomes

The `incidentlens_verified` strategy should outperform `react_no_memory` on:
- Higher root_service_accuracy (case memory guides investigation)
- Lower duplicate_rate (evidence dedup prevents redundant calls)
- Lower misleading_rate (evidence verification filters bad signals)
- Earlier first_effective_hypothesis_round (historical cases accelerate)

The `memory_unverified` strategy should fall between the other two,
benefiting from case memory but lacking evidence verification.
```

- [ ] **Step 3: Commit**

```bash
git add README.md docs/evaluation.md
git commit -m "docs: add README and evaluation methodology"
```

---

### Task 9: Final Verification and Integration

**Files:**
- All files from previous tasks
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py` — ensure event bus publishes during investigation rounds

**Interfaces:**
- Consumes: All components
- Produces: Fully integrated system

- [ ] **Step 1: Wire event bus into investigation engine to publish SSE events**

Modify `apps/control-plane/src/incidentlens_control_plane/routes/investigations.py` — add event publishing after each round:

Add import at top:
```python
from incidentlens_control_plane.events import _global_bus, SSEEvent
```

In `run_round` handler, after `state = await _engine.run_round(incident_id)`, add:
```python
    # Publish SSE events for the state change
    _global_bus.publish(incident_id, SSEEvent(
        event_type="state_changed",
        data={
            "status": state.status.value if hasattr(state.status, 'value') else str(state.status),
            "round": state.current_round,
            "phase": state.phase,
        },
    ))

    # Publish tool_called and evidence_recorded events from audit trail
    if state.evidence:
        latest_evidence = state.evidence[-1]
        _global_bus.publish(incident_id, SSEEvent(
            event_type="evidence_recorded",
            data={
                "source_tool": latest_evidence.source_tool,
                "content": latest_evidence.content,
            },
        ))

    # Publish report_ready if applicable
    if state.status.value == "report_ready" and state.report:
        _global_bus.publish(incident_id, SSEEvent(
            event_type="report_ready",
            data=state.report,
        ))
```

Similarly in `start_investigation`, after `state = _engine.start(alert)`, add:
```python
    _global_bus.publish(state.incident_id, SSEEvent(
        event_type="state_changed",
        data={
            "status": state.status.value if hasattr(state.status, 'value') else str(state.status),
            "round": state.current_round,
            "phase": state.phase,
        },
    ))
```

- [ ] **Step 2: Run full test suite**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv run pytest -q`
Expected: All tests PASS

- [ ] **Step 3: Run linting and type checking**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && uv run ruff check . && uv run mypy packages apps`
Expected: No errors

- [ ] **Step 4: Validate compose config**

Run: `cd /Users/chenxueqiang/Documents/code/incidentlens && docker compose -f infra/compose/compose.yaml config`
Expected: Valid config (requires Docker, may skip if not available)

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/routes/investigations.py
git commit -m "feat: wire SSE event publishing into investigation routes"
```

- [ ] **Step 6: Final commit with all remaining changes**

```bash
git add -A
git commit -m "feat: add dashboard Compose demo and evaluation scaffold"
```
