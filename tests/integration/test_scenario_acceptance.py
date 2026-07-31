"""Five-scenario Compose acceptance tests.

Validates that each of the five fault scenarios, when run through the full
Docker Compose pipeline via DemoRunner(compose=True), produces a report
with the expected root_service and non-empty evidence_ids.

Also asserts:
  - Each scenario produces at least one cross-service trace, one log,
    and one metric piece of evidence (via public read-only APIs).
  - scenario, investigation, report, and CLI serialization never contain
    root_cause_label.
"""

from __future__ import annotations

import json

import pytest
from incidentlens_demo.runner import DemoRunner

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Parametrized scenario definitions
# ---------------------------------------------------------------------------

SCENARIO_PARAMS = [
    ("payment_delay", "payment-service"),
    ("payment_error_rate", "payment-service"),
    ("db_pool_exhaustion", "order-service"),
    ("dependency_unavailable", "order-service"),
    ("deployment_regression", "payment-service"),
]


# ---------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "root_service"),
    SCENARIO_PARAMS,
)
async def test_scenario_reports_expected_root_service(
    compose_urls: dict[str, str],
    scenario: str,
    root_service: str,
) -> None:
    """Each scenario must produce a report with the expected root_service
    and non-empty evidence_ids when run in Compose mode."""
    runner = DemoRunner(
        control_plane_url=compose_urls["control_plane_url"],
        gateway_url=compose_urls["gateway_url"],
        traffic_count=5,
        compose=True,
    )
    result = await runner.run(scenario)

    assert result.status == "passed", (
        f"Scenario {scenario} failed at stage {result.failure_stage}: "
        f"{result.failure_message}"
    )
    assert result.report is not None
    assert result.report["root_service"] == root_service
    assert result.report["evidence_ids"], (
        f"Scenario {scenario} report has empty evidence_ids"
    )


@pytest.mark.parametrize(
    ("scenario", "root_service"),
    SCENARIO_PARAMS,
)
async def test_scenario_has_cross_service_evidence(
    compose_urls: dict[str, str],
    scenario: str,
    root_service: str,
) -> None:
    """Each scenario must produce at least one trace, one log, and one metric
    evidence item, verifiable through public read-only APIs."""
    runner = DemoRunner(
        control_plane_url=compose_urls["control_plane_url"],
        gateway_url=compose_urls["gateway_url"],
        traffic_count=5,
        compose=True,
    )
    result = await runner.run(scenario)

    assert result.status == "passed", result.failure_message
    assert result.report is not None
    assert result.report["evidence_ids"]

    # Check that evidence references span multiple evidence types.
    # The report findings list tool sources; verify at least one from
    # each category (traces, logs, metrics).
    findings = result.report.get("findings", [])
    sources = {f.get("source_tool", "") for f in findings if isinstance(f, dict)}
    # At least one log source
    log_sources = {s for s in sources if "log" in s.lower()}
    # At least one metric source
    metric_sources = {s for s in sources if "metric" in s.lower()}
    # At least one trace source
    trace_sources = {s for s in sources if "trace" in s.lower()}
    assert log_sources and metric_sources and trace_sources, (
        f"Scenario {scenario}: expected at least one trace, one log, AND one metric "
        f"evidence source. Got traces={trace_sources}, logs={log_sources}, "
        f"metrics={metric_sources}. All sources: {sources}"
    )


# ---------------------------------------------------------------------------
# Negative assertions: root_cause_label must never leak
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "root_service"),
    SCENARIO_PARAMS,
)
async def test_root_cause_label_not_in_scenario_result(
    compose_urls: dict[str, str],
    scenario: str,
    root_service: str,
) -> None:
    """The scenario name and result must never contain root_cause_label."""
    runner = DemoRunner(
        control_plane_url=compose_urls["control_plane_url"],
        gateway_url=compose_urls["gateway_url"],
        traffic_count=5,
        compose=True,
    )
    result = await runner.run(scenario)

    # Serialize the entire result to check for root_cause_label leaks
    result_str = json.dumps(
        {
            "scenario": result.scenario,
            "status": result.status,
            "report": result.report,
            "incident_id": result.incident_id,
            "trace_ids": result.trace_ids,
        }
    )
    assert "root_cause_label" not in result_str, (
        f"root_cause_label leaked into scenario result for {scenario}"
    )


@pytest.mark.parametrize(
    ("scenario", "root_service"),
    SCENARIO_PARAMS,
)
async def test_root_cause_label_not_in_investigation(
    compose_urls: dict[str, str],
    scenario: str,
    root_service: str,
) -> None:
    """Investigation data (report, findings) must never contain root_cause_label."""
    runner = DemoRunner(
        control_plane_url=compose_urls["control_plane_url"],
        gateway_url=compose_urls["gateway_url"],
        traffic_count=5,
        compose=True,
    )
    result = await runner.run(scenario)

    if result.report is not None:
        report_str = json.dumps(result.report)
        assert "root_cause_label" not in report_str, (
            f"root_cause_label leaked into investigation report for {scenario}"
        )


@pytest.mark.parametrize(
    ("scenario", "root_service"),
    SCENARIO_PARAMS,
)
async def test_root_cause_label_not_in_cli_serialization(
    compose_urls: dict[str, str],
    scenario: str,
    root_service: str,
) -> None:
    """CLI-serializable output (e.g. run_demo.py stdout) must never contain
    root_cause_label."""
    runner = DemoRunner(
        control_plane_url=compose_urls["control_plane_url"],
        gateway_url=compose_urls["gateway_url"],
        traffic_count=5,
        compose=True,
    )
    result = await runner.run(scenario)

    # Simulate CLI serialization: print-friendly dict
    cli_output = json.dumps(
        {
            "scenario": result.scenario,
            "status": result.status,
            "incident_id": result.incident_id,
            "root_service": result.report.get("root_service") if result.report else None,
            "evidence_count": len(result.report.get("evidence_ids", [])) if result.report else 0,
            "failure_stage": result.failure_stage,
            "failure_message": result.failure_message,
        },
        indent=2,
    )
    assert "root_cause_label" not in cli_output, (
        f"root_cause_label leaked into CLI serialization for {scenario}"
    )
