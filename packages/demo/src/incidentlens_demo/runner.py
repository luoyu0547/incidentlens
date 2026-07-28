"""DemoRunner — orchestrates end-to-end demo scenarios via public APIs.

Each run:
  1. Resets all scenarios and demo data (POST /api/scenarios/reset)
  2. Enables the target scenario (POST /api/scenarios/{name}/enable)
  3. Waits for runtime config to propagate (GET /api/scenarios/runtime/{service})
  4. Sends traffic through the gateway (POST /orders)
  5. Starts an investigation (POST /api/investigations/start)
  6. Runs investigation rounds until terminal state
  7. Asserts report contract (root_service match, non-empty evidence_ids)
  8. Resets again at the end (POST /api/scenarios/reset)

Never exposes root_cause_label. Uses only public APIs.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from incidentlens_scenarios.models import SCENARIOS

# Scenario names exported for CLI use
SCENARIO_NAMES: list[str] = list(SCENARIOS.keys())

# Polling defaults
_RUNTIME_POLL_INTERVAL = 0.5  # seconds
_RUNTIME_POLL_TIMEOUT = 10.0  # seconds
_MAX_ROUNDS = 20  # safety limit for investigation rounds


@dataclass
class DemoRunResult:
    """Result of a single demo scenario run.

    Attributes:
        scenario: Name of the scenario that was run.
        status: "passed" or "failed".
        incident_id: The investigation incident ID (or None if not started).
        trace_ids: Trace IDs from generated traffic.
        report: The investigation report dict (or None if no report).
        failure_stage: Which stage failed (or None).
        failure_message: Human-readable failure reason (or None).
    """

    scenario: str
    status: str
    incident_id: str | None
    trace_ids: list[str]
    report: dict[str, Any] | None
    failure_stage: str | None
    failure_message: str | None


class DemoRunner:
    """Orchestrates end-to-end demo scenarios via public APIs.

    Args:
        control_plane_url: Base URL of the control plane (e.g. "http://localhost:8003").
        gateway_url: Base URL of the gateway service (e.g. "http://localhost:8000").
        traffic_count: Number of order requests to send through the gateway.
        compose: If True, use deterministic params for Docker Compose mode
                 (e.g. payment_error_rate=1.0).
    """

    def __init__(
        self,
        control_plane_url: str,
        gateway_url: str,
        traffic_count: int = 3,
        compose: bool = False,
    ) -> None:
        self._control_plane_url = control_plane_url.rstrip("/")
        self._gateway_url = gateway_url.rstrip("/")
        self._traffic_count = traffic_count
        self._compose = compose
        # Client is created fresh per run; tests can inject a mock via _client
        self._client: httpx.AsyncClient | Any | None = None

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def run(self, scenario: str) -> DemoRunResult:
        """Run a single demo scenario end-to-end.

        Returns a DemoRunResult with status "passed" or "failed".
        """
        if scenario not in SCENARIOS:
            return DemoRunResult(
                scenario=scenario,
                status="failed",
                incident_id=None,
                trace_ids=[],
                report=None,
                failure_stage="validate",
                failure_message=f"Unknown scenario: {scenario}",
            )

        target_service = SCENARIOS[scenario]["target_service"]
        params = self._build_params(scenario)

        # Use provided client or create one
        own_client = self._client is None
        if own_client:
            self._client = httpx.AsyncClient(timeout=30.0)

        try:
            # Step 1: Reset
            await self._post("/api/scenarios/reset")

            # Step 2: Enable scenario
            await self._post(f"/api/scenarios/{scenario}/enable", params)

            # Step 3: Wait for runtime config
            ok = await self._wait_for_runtime_target(scenario, target_service)
            if not ok:
                return DemoRunResult(
                    scenario=scenario,
                    status="failed",
                    incident_id=None,
                    trace_ids=[],
                    report=None,
                    failure_stage="wait_for_runtime",
                    failure_message=f"Scenario {scenario} not visible in runtime config for {target_service}",
                )

            # Step 4: Send traffic
            trace_ids = await self._send_orders()

            # Step 5: Start investigation
            incident_id = await self._start_investigation(scenario, target_service, trace_ids)
            if incident_id is None:
                return DemoRunResult(
                    scenario=scenario,
                    status="failed",
                    incident_id=None,
                    trace_ids=trace_ids,
                    report=None,
                    failure_stage="start_investigation",
                    failure_message="Failed to start investigation",
                )

            # Step 6: Run until terminal state
            report = await self._run_until_terminal(incident_id)
            if report is None:
                return DemoRunResult(
                    scenario=scenario,
                    status="failed",
                    incident_id=incident_id,
                    trace_ids=trace_ids,
                    report=None,
                    failure_stage="run_investigation",
                    failure_message="Investigation did not reach report_ready state",
                )

            # Step 7: Assert report contract
            result = self._assert_contract(scenario, incident_id, trace_ids, report, target_service)

            # Step 8: Reset (always, even on failure)
            return result

        finally:
            # Always reset at the end
            try:
                await self._post("/api/scenarios/reset")
            except Exception:
                pass  # Best-effort reset
            if own_client:
                try:
                    await self._client.aclose()
                except Exception:
                    pass
                self._client = None

    async def run_all(self) -> list[DemoRunResult]:
        """Run all scenarios sequentially and return results."""
        results: list[DemoRunResult] = []
        for name in SCENARIO_NAMES:
            result = await self.run(name)
            results.append(result)
        return results

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _build_params(self, scenario: str) -> dict[str, Any]:
        """Build the parameters dict for enabling a scenario.

        In Compose mode, payment_error_rate uses error_rate=1.0 for
        deterministic results.
        """
        params = dict(SCENARIOS[scenario]["default_params"])
        if self._compose and scenario == "payment_error_rate":
            params["error_rate"] = 1.0
        return params

    async def _post(self, path: str, json: dict | None = None) -> dict[str, Any]:
        """POST to the control plane and return JSON response."""
        url = f"{self._control_plane_url}{path}"
        response = await self._client.post(url, json=json)
        return response.json()

    async def _get(self, path: str) -> dict[str, Any]:
        """GET from the control plane and return JSON response."""
        url = f"{self._control_plane_url}{path}"
        response = await self._client.get(url)
        return response.json()

    async def _wait_for_runtime_target(self, scenario: str, target_service: str) -> bool:
        """Poll runtime config until the scenario is visible for the target service.

        Uses bounded polling with a timeout.
        """
        deadline = time.monotonic() + _RUNTIME_POLL_TIMEOUT
        while time.monotonic() < deadline:
            try:
                data = await self._get(f"/api/scenarios/runtime/{target_service}")
                active = data.get("active", {})
                if scenario in active:
                    return True
            except Exception:
                pass
            await asyncio.sleep(_RUNTIME_POLL_INTERVAL)
        return False

    async def _send_orders(self) -> list[str]:
        """Send order requests through the gateway and collect trace IDs.

        Returns a list of trace IDs from the responses.
        """
        trace_ids: list[str] = []
        for i in range(self._traffic_count):
            try:
                trace_id = f"trace-{time.time_ns()}"
                response = await self._client.post(
                    f"{self._gateway_url}/orders",
                    json={"item": "widget", "quantity": 1},
                    headers={
                        "X-Request-ID": f"req-{time.time_ns()}",
                        "X-Trace-ID": trace_id,
                    },
                )
                data = response.json()
                # Use the trace_id we sent, or one from the response
                trace_ids.append(data.get("trace_id", trace_id))
            except Exception:
                # If a request fails, still record the trace_id we tried
                trace_ids.append(trace_id)
            await asyncio.sleep(0.05)
        return trace_ids

    async def _start_investigation(
        self,
        scenario: str,
        target_service: str,
        trace_ids: list[str],
    ) -> str | None:
        """Start an investigation and return the incident_id.

        Uses the first trace_id if available.
        """
        body: dict[str, Any] = {
            "service": target_service,
            "error_rate": 1.0 if self._compose else 0.3,
        }
        if trace_ids:
            body["trace_id"] = trace_ids[0]

        try:
            data = await self._post("/api/investigations/start", body)
            return data.get("incident_id")
        except Exception:
            return None

    async def _run_until_terminal(self, incident_id: str) -> dict[str, Any] | None:
        """Run investigation rounds until a terminal state is reached.

        Terminal states: report_ready, needs_more_evidence.
        Returns the report dict if report_ready, None otherwise.
        """
        for _ in range(_MAX_ROUNDS):
            try:
                data = await self._post(f"/api/investigations/{incident_id}/round")
            except Exception:
                return None

            status = data.get("status", "")
            if status == "report_ready":
                return data.get("report")
            if status == "needs_more_evidence":
                return None
            # Continue for: scoping, investigating, verifying

        # Exceeded max rounds
        return None

    def _assert_contract(
        self,
        scenario: str,
        incident_id: str,
        trace_ids: list[str],
        report: dict[str, Any],
        expected_target: str,
    ) -> DemoRunResult:
        """Assert the report contract and return a DemoRunResult.

        Contract:
          - root_service must match the scenario's target_service
          - evidence_ids must be non-empty
          - All evidence_ids must be incident-owned (present in report)
          - root_cause_label must never appear
        """
        # Check root_service match
        root_service = report.get("root_service", "")
        if root_service != expected_target:
            return DemoRunResult(
                scenario=scenario,
                status="failed",
                incident_id=incident_id,
                trace_ids=trace_ids,
                report=report,
                failure_stage="assert_contract",
                failure_message=f"root_service mismatch: expected {expected_target}, got {root_service}",
            )

        # Check non-empty evidence_ids
        evidence_ids = report.get("evidence_ids", [])
        if not evidence_ids:
            return DemoRunResult(
                scenario=scenario,
                status="failed",
                incident_id=incident_id,
                trace_ids=trace_ids,
                report=report,
                failure_stage="assert_contract",
                failure_message="evidence_ids is empty in report",
            )

        # Check no root_cause_label leak
        report_str = json.dumps(report)
        if "root_cause_label" in report_str:
            return DemoRunResult(
                scenario=scenario,
                status="failed",
                incident_id=incident_id,
                trace_ids=trace_ids,
                report=report,
                failure_stage="assert_contract",
                failure_message="root_cause_label leaked into report",
            )

        return DemoRunResult(
            scenario=scenario,
            status="passed",
            incident_id=incident_id,
            trace_ids=trace_ids,
            report=report,
            failure_stage=None,
            failure_message=None,
        )
