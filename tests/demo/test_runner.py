"""Tests for the DemoRunner — TDD RED phase.

DemoRunner orchestrates end-to-end demo scenarios via public APIs:
  - POST /api/scenarios/reset
  - POST /api/scenarios/{name}/enable
  - GET /api/scenarios/runtime/{service}
  - POST /orders (gateway traffic)
  - POST /api/investigations/start
  - POST /api/investigations/{incident_id}/round

Each run must:
  - Start and end with an API reset
  - Use public APIs only (never filesystem/SQLite deletion)
  - Return DemoRunResult with status, report, failure details
  - Never expose root_cause_label in any output
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from incidentlens_scenarios.models import SCENARIOS

# ---------------------------------------------------------------------------
# Helpers: mock HTTP responses for each API stage
# ---------------------------------------------------------------------------


def _make_reset_response() -> dict[str, Any]:
    return {"status": "reset", "scenarios_cleared": True}


def _make_enable_response(name: str) -> dict[str, Any]:
    return {"name": name, "active": True, "parameters": {}}


def _make_runtime_response(service: str, scenarios: dict | None = None) -> dict[str, Any]:
    return {"service": service, "active": scenarios or {}}


def _make_order_response(trace_id: str = "trace-1") -> dict[str, Any]:
    return {"order_id": "ord-1", "trace_id": trace_id}


def _make_start_response(incident_id: str = "inc-1") -> dict[str, Any]:
    return {
        "incident_id": incident_id,
        "status": "scoping",
        "current_round": 0,
        "max_rounds": 8,
        "phase": "parse_alert",
        "hypothesis_count": 0,
        "evidence_count": 0,
        "report": None,
    }


def _make_round_response(
    incident_id: str = "inc-1",
    status: str = "report_ready",
    current_round: int = 3,
    root_service: str = "payment-service",
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a round response. Report is auto-generated for report_ready status."""
    # Use provided evidence_ids, or default
    eids = evidence_ids if evidence_ids is not None else ["ev-1", "ev-2"]
    report: dict[str, Any] | None = None
    if status == "report_ready":
        report = {
            "root_service": root_service,
            "root_cause": "payment_service_degradation",
            "evidence_ids": eids,
            "findings": [
                {
                    "evidence_id": eids[0],
                    "source_tool": "search_logs",
                    "content": {},
                }
            ] if eids else [],
            "rounds_completed": current_round,
            "uncertainty": 0.1,
        }
    return {
        "incident_id": incident_id,
        "status": status,
        "current_round": current_round,
        "max_rounds": 8,
        "phase": "generate_report" if status == "report_ready" else "execute_tool",
        "hypothesis_count": 2,
        "evidence_count": len(eids),
        "report": report,
    }


class MockAsyncClient:
    """A mock httpx.AsyncClient that returns preset responses for each URL pattern."""

    def __init__(self, responses: dict[str, list[dict]] | None = None) -> None:
        # Map of URL patterns to lists of responses (consumed in order)
        self.responses: dict[str, list[dict]] = responses or {}
        self._call_log: list[tuple[str, str, dict | None]] = []

    def _match(self, method: str, url: str) -> dict[str, Any]:
        """Find the next matching response for a method+URL."""
        key = f"{method} {url}"
        # Try exact match first
        if key in self.responses and self.responses[key]:
            return self.responses[key].pop(0)
        # Try suffix match (for full URLs that end with the pattern path)
        for pattern, queue in self.responses.items():
            pattern_path = pattern.split(" ", 1)[1]
            if url.endswith(pattern_path) and queue:
                return queue.pop(0)
        # For runtime config polls, return empty active (no scenario visible)
        if "/runtime/" in url:
            return {"service": "", "active": {}}
        # Default: 200 empty
        return {}

    async def post(self, url: str, json: dict | None = None, **kwargs: Any) -> Any:
        self._call_log.append(("POST", url, json))
        resp_data = self._match("POST", url)
        return _MockResponse(200, resp_data)

    async def get(self, url: str, **kwargs: Any) -> Any:
        self._call_log.append(("GET", url, None))
        resp_data = self._match("GET", url)
        return _MockResponse(200, resp_data)

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> MockAsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


class _MockResponse:
    def __init__(self, status_code: int, data: dict[str, Any]) -> None:
        self.status_code = status_code
        self._data = data

    def json(self) -> dict[str, Any]:
        return self._data

    @property
    def text(self) -> str:
        return json.dumps(self._data)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_client() -> MockAsyncClient:
    """Return a MockAsyncClient with default responses for a successful payment_delay run."""
    return MockAsyncClient(responses={
        # Reset
        "POST /api/scenarios/reset": [_make_reset_response(), _make_reset_response()],
        # Enable scenario
        "POST /api/scenarios/payment_delay/enable": [_make_enable_response("payment_delay")],
        # Runtime config check
        "GET /api/scenarios/runtime/payment-service": [
            _make_runtime_response("payment-service", {"payment_delay": {"delay_ms": 200}})
        ],
        # Gateway orders
        "POST /orders": [_make_order_response("trace-1")],
        # Health check
        "GET /healthz": [{"status": "ok"}],
        # Start investigation
        "POST /api/investigations/start": [_make_start_response("inc-1")],
        # Run rounds (3 rounds to report_ready)
        "POST /api/investigations/inc-1/round": [
            _make_round_response("inc-1", "investigating", 1),
            _make_round_response("inc-1", "investigating", 2),
            _make_round_response("inc-1", "report_ready", 3, "payment-service", ["ev-1", "ev-2"]),
        ],
    })


# ---------------------------------------------------------------------------
# Tests: DemoRunner
# ---------------------------------------------------------------------------


class TestDemoRunner:
    """Tests for the DemoRunner orchestration logic."""

    @pytest.mark.asyncio
    async def test_runner_uses_public_api_and_returns_report(
        self, mock_client: MockAsyncClient
    ) -> None:
        """Core TDD test: DemoRunner uses public APIs and returns a passing DemoRunResult."""
        from incidentlens_demo.runner import DemoRunner

        runner = DemoRunner("http://control", "http://gateway", traffic_count=1)
        # Inject mock client
        runner._client = mock_client

        result = await runner.run("payment_delay")

        assert result.status == "passed"
        assert result.report is not None
        assert result.report["root_service"] == "payment-service"
        assert result.report["evidence_ids"]

    @pytest.mark.asyncio
    async def test_runner_resets_before_and_after(self, mock_client: MockAsyncClient) -> None:
        """Runner must call /api/scenarios/reset at start and end."""
        from incidentlens_demo.runner import DemoRunner

        runner = DemoRunner("http://control", "http://gateway", traffic_count=1)
        runner._client = mock_client

        await runner.run("payment_delay")

        # Count reset calls in the log
        reset_calls = [
            (m, u) for m, u, _ in mock_client._call_log
            if m == "POST" and u.endswith("/api/scenarios/reset")
        ]
        assert len(reset_calls) >= 2, (
            f"Expected >=2 reset calls, got {len(reset_calls)}"
        )

    @pytest.mark.asyncio
    async def test_runner_enables_scenario(self, mock_client: MockAsyncClient) -> None:
        """Runner must POST /api/scenarios/{name}/enable with default params."""
        from incidentlens_demo.runner import DemoRunner

        runner = DemoRunner("http://control", "http://gateway", traffic_count=1)
        runner._client = mock_client

        await runner.run("payment_delay")

        enable_calls = [
            (m, u, body) for m, u, body in mock_client._call_log
            if m == "POST" and "/enable" in u
        ]
        assert len(enable_calls) >= 1
        # URL should contain the scenario name
        assert "payment_delay" in enable_calls[0][1]

    @pytest.mark.asyncio
    async def test_runner_starts_investigation_with_trace_ids(
        self, mock_client: MockAsyncClient
    ) -> None:
        """Runner must start investigation with trace_ids from traffic."""
        from incidentlens_demo.runner import DemoRunner

        runner = DemoRunner("http://control", "http://gateway", traffic_count=1)
        runner._client = mock_client

        await runner.run("payment_delay")

        start_calls = [
            body for m, u, body in mock_client._call_log
            if m == "POST" and u.endswith("/api/investigations/start") and body is not None
        ]
        assert len(start_calls) >= 1
        # The start call should reference the target service
        assert start_calls[0]["service"] is not None

    @pytest.mark.asyncio
    async def test_runner_returns_failure_on_wrong_root_service(self) -> None:
        """If root_service doesn't match scenario target, result should be 'failed'."""
        from incidentlens_demo.runner import DemoRunner

        client = MockAsyncClient(responses={
            "POST /api/scenarios/reset": [_make_reset_response(), _make_reset_response()],
            "POST /api/scenarios/payment_delay/enable": [_make_enable_response("payment_delay")],
            "GET /api/scenarios/runtime/payment-service": [
                _make_runtime_response("payment-service", {"payment_delay": {"delay_ms": 200}})
            ],
            "POST /orders": [_make_order_response("trace-1")],
            "GET /healthz": [{"status": "ok"}],
            "POST /api/investigations/start": [_make_start_response("inc-1")],
            "POST /api/investigations/inc-1/round": [
                _make_round_response(
                    "inc-1", "report_ready", 3,
                    root_service="order-service",  # Wrong! payment_delay targets payment-service
                    evidence_ids=["ev-1"],
                ),
            ],
        })

        runner = DemoRunner("http://control", "http://gateway", traffic_count=1)
        runner._client = client

        result = await runner.run("payment_delay")
        assert result.status == "failed"
        msg = result.failure_message or ""
        assert "root_service" in msg.lower() or "mismatch" in msg.lower()

    @pytest.mark.asyncio
    async def test_runner_returns_failure_on_empty_evidence_ids(self) -> None:
        """If evidence_ids is empty in report, result should be 'failed'."""
        from incidentlens_demo.runner import DemoRunner

        client = MockAsyncClient(responses={
            "POST /api/scenarios/reset": [_make_reset_response(), _make_reset_response()],
            "POST /api/scenarios/payment_delay/enable": [_make_enable_response("payment_delay")],
            "GET /api/scenarios/runtime/payment-service": [
                _make_runtime_response("payment-service", {"payment_delay": {"delay_ms": 200}})
            ],
            "POST /orders": [_make_order_response("trace-1")],
            "GET /healthz": [{"status": "ok"}],
            "POST /api/investigations/start": [_make_start_response("inc-1")],
            "POST /api/investigations/inc-1/round": [
                _make_round_response(
                    "inc-1", "report_ready", 3,
                    root_service="payment-service",
                    evidence_ids=[],  # Empty!
                ),
            ],
        })

        runner = DemoRunner("http://control", "http://gateway", traffic_count=1)
        runner._client = client

        result = await runner.run("payment_delay")
        assert result.status == "failed"
        assert result.failure_stage is not None

    @pytest.mark.asyncio
    async def test_runner_returns_failure_on_needs_more_evidence(self) -> None:
        """If investigation ends in 'needs_more_evidence', result should be 'failed'."""
        from incidentlens_demo.runner import DemoRunner

        client = MockAsyncClient(responses={
            "POST /api/scenarios/reset": [_make_reset_response(), _make_reset_response()],
            "POST /api/scenarios/payment_delay/enable": [_make_enable_response("payment_delay")],
            "GET /api/scenarios/runtime/payment-service": [
                _make_runtime_response("payment-service", {"payment_delay": {"delay_ms": 200}})
            ],
            "POST /orders": [_make_order_response("trace-1")],
            "GET /healthz": [{"status": "ok"}],
            "POST /api/investigations/start": [_make_start_response("inc-1")],
            "POST /api/investigations/inc-1/round": [
                _make_round_response("inc-1", "investigating", 1),
                _make_round_response("inc-1", "investigating", 2),
                # Hit max rounds without report
                _make_round_response("inc-1", "needs_more_evidence", 8),
            ],
        })

        runner = DemoRunner("http://control", "http://gateway", traffic_count=1)
        runner._client = client

        result = await runner.run("payment_delay")
        assert result.status == "failed"

    @pytest.mark.asyncio
    async def test_runner_never_exposes_root_cause_label(
        self, mock_client: MockAsyncClient
    ) -> None:
        """DemoRunResult must never contain root_cause_label."""
        from incidentlens_demo.runner import DemoRunner

        runner = DemoRunner("http://control", "http://gateway", traffic_count=1)
        runner._client = mock_client

        result = await runner.run("payment_delay")

        result_str = json.dumps(result.__dict__ if hasattr(result, "__dict__") else str(result))
        assert "root_cause_label" not in result_str

    @pytest.mark.asyncio
    async def test_run_all_returns_list(self) -> None:
        """run_all() should return a list of DemoRunResult for each scenario."""
        from incidentlens_demo.runner import SCENARIO_NAMES, DemoRunner

        # Build a mock client with enough responses for all scenarios
        n = len(SCENARIO_NAMES)
        client = MockAsyncClient(responses={
            "POST /api/scenarios/reset": [_make_reset_response() for _ in range(n * 2 + 2)],
            "POST /orders": [_make_order_response(f"trace-{i}") for i in range(n * 3)],
            "GET /healthz": [{"status": "ok"} for _ in range(n)],
        })
        for name in SCENARIO_NAMES:
            client.responses[f"POST /api/scenarios/{name}/enable"] = [_make_enable_response(name)]
            target = SCENARIOS[name]["target_service"]
            # Add runtime response (may need multiple for same service)
            key = f"GET /api/scenarios/runtime/{target}"
            if key not in client.responses:
                client.responses[key] = []
            client.responses[key].append(
                _make_runtime_response(target, {name: SCENARIOS[name]["default_params"]})
            )
        # Investigation responses: one start + one round per scenario
        client.responses["POST /api/investigations/start"] = [
            _make_start_response(f"inc-{i}") for i in range(n)
        ]
        for i in range(n):
            name = list(SCENARIO_NAMES)[i]
            target = SCENARIOS[name]["target_service"]
            client.responses[f"POST /api/investigations/inc-{i}/round"] = [
                _make_round_response(f"inc-{i}", "report_ready", 3, target, ["ev-1"]),
            ]

        runner = DemoRunner("http://control", "http://gateway", traffic_count=1)
        runner._client = client

        results = await runner.run_all()
        assert isinstance(results, list)
        assert len(results) == len(SCENARIO_NAMES)

    @pytest.mark.asyncio
    async def test_runner_uses_payment_error_rate_1_for_compose(self) -> None:
        """In Compose mode (compose=True), payment_error_rate should use error_rate=1.0."""
        from incidentlens_demo.runner import DemoRunner

        client = MockAsyncClient(responses={
            "POST /api/scenarios/reset": [_make_reset_response(), _make_reset_response()],
            "POST /api/scenarios/payment_error_rate/enable": [
                _make_enable_response("payment_error_rate")
            ],
            "GET /api/scenarios/runtime/payment-service": [
                _make_runtime_response(
                    "payment-service",
                    {"payment_error_rate": {"error_rate": 1.0}},
                )
            ],
            "POST /orders": [_make_order_response("trace-1")],
            "GET /healthz": [{"status": "ok"}],
            "POST /api/investigations/start": [_make_start_response("inc-1")],
            "POST /api/investigations/inc-1/round": [
                _make_round_response("inc-1", "report_ready", 3, "payment-service", ["ev-1"]),
            ],
        })

        runner = DemoRunner("http://control", "http://gateway", traffic_count=1, compose=True)
        runner._client = client

        await runner.run("payment_error_rate")

        enable_calls = [
            body for m, u, body in client._call_log
            if m == "POST" and "/enable" in u and body is not None
        ]
        assert len(enable_calls) >= 1
        assert enable_calls[0].get("error_rate") == 1.0

    @pytest.mark.asyncio
    async def test_runner_records_trace_ids(self, mock_client: MockAsyncClient) -> None:
        """DemoRunResult should contain trace_ids from traffic generation."""
        from incidentlens_demo.runner import DemoRunner

        runner = DemoRunner("http://control", "http://gateway", traffic_count=1)
        runner._client = mock_client

        result = await runner.run("payment_delay")
        assert result.trace_ids is not None
        assert len(result.trace_ids) >= 1

    def test_llm_runner_uses_twenty_minute_investigation_timeout(self) -> None:
        from incidentlens_demo.runner import DemoRunner

        runner = DemoRunner(
            control_plane_url="http://control-plane",
            gateway_url="http://gateway",
            mode="llm_agent",
        )
        assert runner.investigation_timeout_seconds == 1200

    def test_baseline_runner_uses_short_timeout(self) -> None:
        from incidentlens_demo.runner import DemoRunner

        runner = DemoRunner(
            control_plane_url="http://control-plane",
            gateway_url="http://gateway",
            mode="deterministic_baseline",
        )
        assert runner.investigation_timeout_seconds == 30
