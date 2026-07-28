"""Tests for the run_demo CLI script — TDD RED phase.

CLI must:
  - Accept mutually exclusive --scenario NAME and --all
  - Accept --control-plane-url, --gateway-url, --traffic-count
  - Print scenario, stage, incident ID, root service, cause, evidence IDs
  - Return nonzero exit code on any failed scenario
  - Never expose root_cause_label in output
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from incidentlens_demo.runner import DemoRunResult


# ---------------------------------------------------------------------------
# Import the run_demo script as a module
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "run_demo.py"


def _load_run_demo() -> Any:
    """Load scripts/run_demo.py as a module."""
    spec = importlib.util.spec_from_file_location("run_demo", str(_SCRIPT_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _passing_result(scenario: str = "payment_delay") -> DemoRunResult:
    return DemoRunResult(
        scenario=scenario,
        status="passed",
        incident_id="inc-1",
        trace_ids=["trace-1"],
        report={
            "root_service": "payment-service",
            "root_cause": "payment_service_degradation",
            "evidence_ids": ["ev-1", "ev-2"],
            "findings": [],
            "rounds_completed": 3,
            "uncertainty": 0.1,
        },
        failure_stage=None,
        failure_message=None,
    )


def _failing_result(scenario: str = "payment_delay") -> DemoRunResult:
    return DemoRunResult(
        scenario=scenario,
        status="failed",
        incident_id="inc-2",
        trace_ids=["trace-2"],
        report=None,
        failure_stage="assert_contract",
        failure_message="root_service mismatch: expected payment-service, got order-service",
    )


class FakePassingRunner:
    """A fake DemoRunner that always returns passing results."""

    def __init__(self, control_plane_url: str, gateway_url: str, traffic_count: int = 3, **kwargs: Any) -> None:
        self.control_plane_url = control_plane_url
        self.gateway_url = gateway_url
        self.traffic_count = traffic_count

    async def run(self, scenario: str) -> DemoRunResult:
        return _passing_result(scenario)

    async def run_all(self) -> list[DemoRunResult]:
        from incidentlens_demo.runner import SCENARIO_NAMES
        return [_passing_result(name) for name in SCENARIO_NAMES]


class FakeFailingRunner:
    """A fake DemoRunner that returns a failing result for one scenario."""

    def __init__(self, control_plane_url: str, gateway_url: str, traffic_count: int = 3, **kwargs: Any) -> None:
        self.control_plane_url = control_plane_url
        self.gateway_url = gateway_url
        self.traffic_count = traffic_count

    async def run(self, scenario: str) -> DemoRunResult:
        return _failing_result(scenario)

    async def run_all(self) -> list[DemoRunResult]:
        from incidentlens_demo.runner import SCENARIO_NAMES
        results = []
        for name in SCENARIO_NAMES:
            if name == "deployment_regression":
                results.append(_failing_result(name))
            else:
                results.append(_passing_result(name))
        return results


# ---------------------------------------------------------------------------
# Tests: CLI
# ---------------------------------------------------------------------------


class TestRunDemoCLI:
    """Tests for the run_demo.py CLI script."""

    def test_cli_all_prints_each_scenario(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """--all should print each scenario name with status."""
        run_demo = _load_run_demo()
        monkeypatch.setattr(run_demo, "DemoRunner", FakePassingRunner)
        assert run_demo.main(["--all"]) == 0
        output = capsys.readouterr().out
        assert "deployment_regression: passed" in output

    def test_cli_scenario_prints_result(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """--scenario NAME should print the scenario result."""
        run_demo = _load_run_demo()
        monkeypatch.setattr(run_demo, "DemoRunner", FakePassingRunner)
        assert run_demo.main(["--scenario", "payment_delay"]) == 0
        output = capsys.readouterr().out
        assert "payment_delay: passed" in output

    def test_cli_nonzero_exit_on_failure(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """CLI should return nonzero exit code when any scenario fails."""
        run_demo = _load_run_demo()
        monkeypatch.setattr(run_demo, "DemoRunner", FakeFailingRunner)
        result = run_demo.main(["--scenario", "payment_delay"])
        assert result != 0

    def test_cli_all_nonzero_on_any_failure(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """--all should return nonzero when any scenario fails."""
        run_demo = _load_run_demo()
        monkeypatch.setattr(run_demo, "DemoRunner", FakeFailingRunner)
        result = run_demo.main(["--all"])
        assert result != 0

    def test_cli_mutually_exclusive_scenario_and_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--scenario and --all are mutually exclusive."""
        run_demo = _load_run_demo()
        monkeypatch.setattr(run_demo, "DemoRunner", FakePassingRunner)
        # SystemExit(2) for argparse error
        with pytest.raises(SystemExit) as exc_info:
            run_demo.main(["--scenario", "payment_delay", "--all"])
        assert exc_info.value.code == 2

    def test_cli_requires_scenario_or_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLI requires either --scenario or --all."""
        run_demo = _load_run_demo()
        monkeypatch.setattr(run_demo, "DemoRunner", FakePassingRunner)
        with pytest.raises(SystemExit) as exc_info:
            run_demo.main([])
        assert exc_info.value.code == 2

    def test_cli_prints_incident_id_and_root_service(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """CLI output should include incident_id and root_service."""
        run_demo = _load_run_demo()
        monkeypatch.setattr(run_demo, "DemoRunner", FakePassingRunner)
        run_demo.main(["--scenario", "payment_delay"])
        output = capsys.readouterr().out
        assert "inc-1" in output
        assert "payment-service" in output

    def test_cli_prints_evidence_ids(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """CLI output should include evidence IDs."""
        run_demo = _load_run_demo()
        monkeypatch.setattr(run_demo, "DemoRunner", FakePassingRunner)
        run_demo.main(["--scenario", "payment_delay"])
        output = capsys.readouterr().out
        assert "ev-1" in output

    def test_cli_never_exposes_root_cause_label(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """CLI output must never contain root_cause_label."""
        run_demo = _load_run_demo()
        monkeypatch.setattr(run_demo, "DemoRunner", FakePassingRunner)
        run_demo.main(["--all"])
        output = capsys.readouterr().out
        assert "root_cause_label" not in output

    def test_cli_accepts_url_and_traffic_count_options(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """CLI should accept --control-plane-url, --gateway-url, --traffic-count."""
        run_demo = _load_run_demo()
        monkeypatch.setattr(run_demo, "DemoRunner", FakePassingRunner)
        result = run_demo.main([
            "--scenario", "payment_delay",
            "--control-plane-url", "http://cp:8003",
            "--gateway-url", "http://gw:8000",
            "--traffic-count", "5",
        ])
        assert result == 0

    def test_cli_invalid_scenario_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--scenario with invalid name should fail."""
        run_demo = _load_run_demo()
        monkeypatch.setattr(run_demo, "DemoRunner", FakePassingRunner)
        with pytest.raises(SystemExit) as exc_info:
            run_demo.main(["--scenario", "nonexistent_scenario"])
        assert exc_info.value.code == 2
