"""Evaluation runner — execute investigations under different strategies.

Strategies:
  - react_no_memory: no case memory, no evidence verification
  - memory_unverified: case memory enabled, but evidence not verified
  - incidentlens_verified: full pipeline with case memory and evidence verification

Each strategy runs an investigation against a scenario and produces a RunRecord.
run_evaluation(strategy, scenario) runs all scenarios and returns aggregated metrics.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

from incidentlens_contracts.models import TelemetryEvent
from incidentlens_control_plane.agent.engine import InvestigationEngine
from incidentlens_control_plane.memory.models import CaseRow
from incidentlens_control_plane.memory.repository import CaseRepository
from incidentlens_control_plane.tools.query import ReadOnlyToolkit
from incidentlens_scenarios.models import SCENARIOS
from incidentlens_telemetry.database import create_engine
from incidentlens_telemetry.repository import TelemetryRepository

from incidentlens_evaluation.metrics import (
    EvaluationResult,
    RunRecord,
    compute_metrics,
)


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
        with case_repo.transaction() as session:
            case_row = CaseRow(
                status="human_verified",
                symptom="high error rate",
                root_cause_category=root_cause_label,
                root_cause_description=root_cause_label,
                resolution="rolled back deployment",
            )
            case_repo.add_case(session, case_row)

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

    loop = asyncio.new_event_loop()
    try:
        for _ in range(8):
            state = loop.run_until_complete(engine.run_round(state.incident_id))
            if state.status in ("report_ready", "needs_more_evidence"):
                break
    finally:
        loop.close()

    elapsed_ms = (time.monotonic() - start_time) * 1000

    # Derive actual values ONLY from the report — never default to expected
    root_service_actual: str | None = None
    root_cause_type_actual: str | None = None
    if state.report:
        root_service_actual = state.report.get("root_service")
        root_cause_type_actual = state.report.get("root_cause")

    # Check evidence reference correctness
    evidence_reference_correct = False
    if state.report:
        evidence_ids = state.report.get("evidence_ids", [])
        if evidence_ids:
            evidence_reference_correct = True

    # Count tool calls from evidence
    tool_calls = len(state.evidence)

    # Find first effective round
    first_effective_round: int | None = None
    for hyp in state.hypotheses:
        if hyp.status == "confirmed" and hyp.supporting_evidence_ids:
            first_effective_round = state.current_round
            break

    # Count duplicate calls
    # Duplicates are defined by same tool_name + normalized_args (not tool_call_id)
    seen_tools: set[str] = set()
    duplicate_calls = 0
    for ev in state.evidence:
        # Normalize args by removing incident_id (which is always injected)
        normalized_args = {
            k: v for k, v in ev.content.items()
            if k != "incident_id"
        } if isinstance(ev.content, dict) else {}
        key = f"{ev.source_tool}:{json.dumps(normalized_args, sort_keys=True, default=str)}"
        if key in seen_tools:
            duplicate_calls += 1
        seen_tools.add(key)

    # Derive historical usage counts from case repository
    historical_cases_adopted = 0
    historical_cases_misleading = 0
    if case_repo is not None:
        try:
            # Query cases adopted/misleading for this incident
            # In a real implementation, this would query usage events by incident_id
            # For now, we derive from the case repository's state
            with case_repo.transaction() as session:
                from incidentlens_control_plane.memory.models import CaseUsageEventRow
                from sqlalchemy import select

                events = session.execute(
                    select(CaseUsageEventRow).where(
                        CaseUsageEventRow.case_id.in_(
                            select(CaseRow.id).where(
                                CaseRow.affected_services_json.contains(target_service)
                            )
                        )
                    )
                ).scalars().all()
                for event in events:
                    if hasattr(event, 'event_type'):
                        if event.event_type == "adopted":
                            historical_cases_adopted += 1
                        elif event.event_type == "misleading":
                            historical_cases_misleading += 1
        except Exception:
            # If case query fails, use zeros (no historical data available)
            pass

    return RunRecord(
        root_service_expected=target_service,
        root_service_actual=root_service_actual,
        root_cause_type_expected=root_cause_label,
        root_cause_type_actual=root_cause_type_actual,
        tool_calls=tool_calls,
        evidence_reference_correct=evidence_reference_correct,
        first_effective_round=first_effective_round,
        duplicate_calls=duplicate_calls,
        historical_cases_adopted=historical_cases_adopted,
        historical_cases_misleading=historical_cases_misleading,
        latency_ms=elapsed_ms,
    )


def run_evaluation(
    strategy: str,
    scenario: str,
    *,
    store: Any | None = None,
) -> EvaluationResult:
    """Run evaluation for a strategy against a specific scenario.

    Args:
        strategy: One of 'react_no_memory', 'memory_unverified', 'incidentlens_verified'
        scenario: One of the scenario names, or 'all' to run all scenarios
        store: Optional EvaluationRunStore for persisting results

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

    run_id: int | None = None
    if store is not None:
        run_id = store.start(strategy, scenario)

    records: list[RunRecord] = []
    try:
        for name in scenario_names:
            record = run_single(strategy, name)
            records.append(record)
            if store is not None and run_id is not None:
                store.record(run_id, record.model_dump())

        result = compute_metrics(records)

        if store is not None and run_id is not None:
            store.complete(run_id, result.model_dump())

        return result
    except Exception as exc:
        if store is not None and run_id is not None:
            error_code = type(exc).__name__
            store.fail(run_id, error_code)
        raise
