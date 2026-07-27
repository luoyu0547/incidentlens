"""Tests for audited read-only tools — TDD RED phase.

These tests define the desired interface for the control-plane tools:
  - query_metrics       — filter metric points by service, name, time range
  - search_logs         — filter log rows by service, keyword
  - get_slow_traces     — find traces with slow spans
  - get_trace           — aggregate all spans for a given trace_id
  - get_service_dependencies — derive service dependency graph from spans
  - list_recent_deployments  — list recent deployments for a service
  - get_runbook         — retrieve a runbook for a service

All tools must:
  - Return ToolResult[Any] (never throw unhandled exceptions)
  - Enforce limits: max 24h time range, max 100 results, max 16 KiB text
  - Record every invocation in tool_audits table
  - Have timeout_seconds=3, max_retries=1
  - Be read-only (no write operations)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from incidentlens_contracts.models import TelemetryEvent, ToolResult
from incidentlens_telemetry.database import create_engine
from incidentlens_telemetry.repository import TelemetryRepository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def repository():
    """Create a TelemetryRepository backed by an in-memory SQLite DB."""
    engine = create_engine("sqlite:///:memory:")
    return TelemetryRepository(engine)


@pytest.fixture()
def toolkit(repository):
    """Create a ReadOnlyToolkit with the in-memory repository."""
    from incidentlens_control_plane.tools.query import ReadOnlyToolkit

    return ReadOnlyToolkit(repository)


@pytest.fixture()
def audit_store(repository):
    """Provide access to the audit store for checking audit records."""
    from incidentlens_control_plane.tools.base import AuditStore

    engine = repository.engine
    return AuditStore(engine)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(minute: int = 0) -> datetime:
    """Return an aware datetime at 2025-01-01 00:{minute}:00 UTC."""
    return datetime(2025, 1, 1, 0, minute, tzinfo=timezone.utc)


def _log_event(
    service: str = "order-service",
    trace_id: str = "trace-a",
    level: str = "ERROR",
    message: str = "payment failed",
    minute: int = 0,
) -> TelemetryEvent:
    return TelemetryEvent(
        event_type="log",
        service=service,
        trace_id=trace_id,
        occurred_at=_ts(minute),
        payload={"level": level, "message": message},
    )


def _metric_event(
    service: str = "order-service",
    trace_id: str = "trace-a",
    name: str = "request_duration_ms",
    value: float = 120.5,
    minute: int = 0,
) -> TelemetryEvent:
    return TelemetryEvent(
        event_type="metric",
        service=service,
        trace_id=trace_id,
        occurred_at=_ts(minute),
        payload={"name": name, "value": value},
    )


def _span_event(
    service: str = "order-service",
    trace_id: str = "trace-a",
    span_id: str = "span-1",
    parent_id: str | None = None,
    operation: str = "POST /checkout",
    minute: int = 0,
) -> TelemetryEvent:
    payload: dict = {"span_id": span_id, "operation": operation}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    return TelemetryEvent(
        event_type="span",
        service=service,
        trace_id=trace_id,
        occurred_at=_ts(minute),
        payload=payload,
    )


def _deployment_event(
    service: str = "order-service",
    version: str = "v2.3.1",
    minute: int = 0,
) -> TelemetryEvent:
    return TelemetryEvent(
        event_type="deployment",
        service=service,
        trace_id="",
        occurred_at=_ts(minute),
        payload={"version": version},
    )


# ===================================================================
# SEARCH LOGS — limit, audit, keyword
# ===================================================================


class TestSearchLogs:
    """Tests for search_logs tool."""

    @pytest.mark.asyncio
    async def test_search_logs_is_limited_and_audited(
        self, toolkit, audit_store
    ) -> None:
        """Core TDD test: search_logs returns limited results and is audited."""
        repository = toolkit._repository
        repository.record(_log_event(message="timeout at payment"))
        repository.record(_log_event(message="connection refused"))

        result = await toolkit.search_logs(
            service="order-service", keyword="timeout"
        )
        assert result.ok
        assert result.metadata["limit"] <= 100
        assert audit_store.latest().tool_name == "search_logs"

    @pytest.mark.asyncio
    async def test_search_logs_returns_matching_logs(self, toolkit) -> None:
        repository = toolkit._repository
        repository.record(_log_event(message="timeout at payment"))
        repository.record(_log_event(message="connection refused"))
        repository.record(_log_event(message="timeout retry"))

        result = await toolkit.search_logs(
            service="order-service", keyword="timeout"
        )
        assert result.ok
        assert len(result.data) == 2

    @pytest.mark.asyncio
    async def test_search_logs_empty_result_returns_tool_result(
        self, toolkit
    ) -> None:
        result = await toolkit.search_logs(
            service="nonexistent", keyword="anything"
        )
        assert result.ok
        assert result.data == []

    @pytest.mark.asyncio
    async def test_search_logs_respects_limit(self, toolkit) -> None:
        repository = toolkit._repository
        for i in range(150):
            repository.record(_log_event(message=f"timeout msg-{i}"))

        result = await toolkit.search_logs(
            service="order-service", keyword="timeout", limit=50
        )
        assert result.ok
        assert len(result.data) <= 50

    @pytest.mark.asyncio
    async def test_search_logs_rejects_limit_over_100(self, toolkit) -> None:
        result = await toolkit.search_logs(
            service="order-service", keyword="timeout", limit=200
        )
        # Should clamp to 100, not fail
        assert result.ok
        assert result.metadata["limit"] <= 100


# ===================================================================
# QUERY METRICS — time range, limit, audit
# ===================================================================


class TestQueryMetrics:
    """Tests for query_metrics tool."""

    @pytest.mark.asyncio
    async def test_query_metrics_returns_filtered_results(
        self, toolkit, audit_store
    ) -> None:
        repository = toolkit._repository
        repository.record(
            _metric_event(name="request_duration_ms", value=120.5, minute=10)
        )
        repository.record(
            _metric_event(name="error_rate", value=0.01, minute=20)
        )

        result = await toolkit.query_metrics(
            service="order-service",
            name="request_duration_ms",
            start=_ts(5),
            end=_ts(15),
        )
        assert result.ok
        assert len(result.data) == 1
        assert result.data[0]["value"] == 120.5
        assert audit_store.latest().tool_name == "query_metrics"

    @pytest.mark.asyncio
    async def test_query_metrics_rejects_time_range_over_24h(
        self, toolkit
    ) -> None:
        start = _ts(0)
        end = start + timedelta(hours=25)

        result = await toolkit.query_metrics(
            service="order-service",
            start=start,
            end=end,
        )
        assert result.ok is False
        assert "time range" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_query_metrics_empty_result(self, toolkit) -> None:
        result = await toolkit.query_metrics(service="nonexistent")
        assert result.ok
        assert result.data == []


# ===================================================================
# GET SLOW TRACES
# ===================================================================


class TestGetSlowTraces:
    """Tests for get_slow_traces tool."""

    @pytest.mark.asyncio
    async def test_get_slow_traces_returns_traces_with_slow_spans(
        self, toolkit, audit_store
    ) -> None:
        repository = toolkit._repository
        # Create a trace with a slow span (duration > threshold)
        repository.record(
            _span_event(
                trace_id="slow-trace",
                span_id="span-slow",
                operation="POST /checkout",
                minute=0,
            )
        )
        repository.record(
            _span_event(
                trace_id="slow-trace",
                span_id="span-slow-end",
                operation="POST /checkout",
                minute=5,  # 5 minutes = slow
            )
        )

        result = await toolkit.get_slow_traces(
            service="order-service", threshold_seconds=60
        )
        assert result.ok
        assert audit_store.latest().tool_name == "get_slow_traces"

    @pytest.mark.asyncio
    async def test_get_slow_traces_empty_result(self, toolkit) -> None:
        result = await toolkit.get_slow_traces(
            service="nonexistent", threshold_seconds=60
        )
        assert result.ok
        assert result.data == []


# ===================================================================
# GET TRACE
# ===================================================================


class TestGetTrace:
    """Tests for get_trace tool."""

    @pytest.mark.asyncio
    async def test_get_trace_returns_aggregated_spans(
        self, toolkit, audit_store
    ) -> None:
        repository = toolkit._repository
        repository.record(
            _span_event(span_id="span-1", operation="POST /checkout")
        )
        repository.record(
            _span_event(
                span_id="span-2", parent_id="span-1", operation="DB insert"
            )
        )

        result = await toolkit.get_trace(trace_id="trace-a")
        assert result.ok
        assert result.data is not None
        assert len(result.data["spans"]) == 2
        assert audit_store.latest().tool_name == "get_trace"

    @pytest.mark.asyncio
    async def test_get_trace_not_found_returns_tool_result(self, toolkit) -> None:
        result = await toolkit.get_trace(trace_id="nonexistent")
        assert result.ok
        assert result.data is None

    @pytest.mark.asyncio
    async def test_get_trace_rejects_long_trace_id(self, toolkit) -> None:
        result = await toolkit.get_trace(trace_id="x" * 20000)
        assert result.ok is False


# ===================================================================
# GET SERVICE DEPENDENCIES
# ===================================================================


class TestGetServiceDependencies:
    """Tests for get_service_dependencies tool."""

    @pytest.mark.asyncio
    async def test_get_service_dependencies_returns_graph(
        self, toolkit, audit_store
    ) -> None:
        repository = toolkit._repository
        # Create spans showing order-service calling payment-service
        repository.record(
            _span_event(
                service="order-service",
                trace_id="trace-dep",
                span_id="span-order",
                operation="POST /checkout",
            )
        )
        repository.record(
            _span_event(
                service="payment-service",
                trace_id="trace-dep",
                span_id="span-payment",
                parent_id="span-order",
                operation="POST /charge",
            )
        )

        result = await toolkit.get_service_dependencies()
        assert result.ok
        assert result.data is not None
        assert audit_store.latest().tool_name == "get_service_dependencies"

    @pytest.mark.asyncio
    async def test_get_service_dependencies_empty(self, toolkit) -> None:
        result = await toolkit.get_service_dependencies()
        assert result.ok
        assert result.data == []


# ===================================================================
# LIST RECENT DEPLOYMENTS
# ===================================================================


class TestListRecentDeployments:
    """Tests for list_recent_deployments tool."""

    @pytest.mark.asyncio
    async def test_list_recent_deployments_returns_deployments(
        self, toolkit, audit_store
    ) -> None:
        repository = toolkit._repository
        repository.record(
            _deployment_event(service="order-service", version="v2.3.1")
        )
        repository.record(
            _deployment_event(service="order-service", version="v2.3.2")
        )

        result = await toolkit.list_recent_deployments(
            service="order-service"
        )
        assert result.ok
        assert len(result.data) == 2
        assert audit_store.latest().tool_name == "list_recent_deployments"

    @pytest.mark.asyncio
    async def test_list_recent_deployments_empty(self, toolkit) -> None:
        result = await toolkit.list_recent_deployments(
            service="nonexistent"
        )
        assert result.ok
        assert result.data == []

    @pytest.mark.asyncio
    async def test_list_recent_deployments_respects_limit(
        self, toolkit
    ) -> None:
        repository = toolkit._repository
        for i in range(150):
            repository.record(
                _deployment_event(
                    service="order-service", version=f"v1.{i}"
                )
            )

        result = await toolkit.list_recent_deployments(
            service="order-service", limit=50
        )
        assert result.ok
        assert len(result.data) <= 50


# ===================================================================
# GET RUNBOOK
# ===================================================================


class TestGetRunbook:
    """Tests for get_runbook tool."""

    @pytest.mark.asyncio
    async def test_get_runbook_returns_runbook_for_service(
        self, toolkit, audit_store
    ) -> None:
        result = await toolkit.get_runbook(service="order-service")
        assert result.ok
        assert result.data is not None
        assert audit_store.latest().tool_name == "get_runbook"

    @pytest.mark.asyncio
    async def test_get_runbook_unknown_service_returns_empty(
        self, toolkit
    ) -> None:
        result = await toolkit.get_runbook(service="unknown-service")
        assert result.ok is False
        assert "No runbook found" in (result.error or "")


# ===================================================================
# AUDIT TRAIL — all tools must be audited
# ===================================================================


class TestAuditTrail:
    """Tests for the audit trail mechanism."""

    @pytest.mark.asyncio
    async def test_audit_records_parameters(self, toolkit, audit_store) -> None:
        await toolkit.search_logs(service="order-service", keyword="timeout")
        audit = audit_store.latest()
        assert audit.tool_name == "search_logs"
        assert "order-service" in str(audit.parameters)

    @pytest.mark.asyncio
    async def test_audit_records_duration(self, toolkit, audit_store) -> None:
        await toolkit.search_logs(service="order-service", keyword="timeout")
        audit = audit_store.latest()
        assert audit.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_audit_records_retries(self, toolkit, audit_store) -> None:
        await toolkit.search_logs(service="order-service", keyword="timeout")
        audit = audit_store.latest()
        assert audit.retries == 0

    @pytest.mark.asyncio
    async def test_audit_records_error_on_failure(
        self, toolkit, audit_store
    ) -> None:
        # Trigger a validation failure
        result = await toolkit.query_metrics(
            service="order-service",
            start=_ts(0),
            end=_ts(0) + timedelta(hours=25),
        )
        assert result.ok is False
        audit = audit_store.latest()
        assert audit.error is not None


# ===================================================================
# INPUT VALIDATION — limits enforced
# ===================================================================


class TestInputValidation:
    """Tests for input validation limits."""

    @pytest.mark.asyncio
    async def test_time_range_max_24_hours(self, toolkit) -> None:
        start = _ts(0)
        end = start + timedelta(hours=25)
        result = await toolkit.query_metrics(
            service="order-service", start=start, end=end
        )
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_time_range_exactly_24_hours_is_ok(self, toolkit) -> None:
        start = _ts(0)
        end = start + timedelta(hours=24)
        result = await toolkit.query_metrics(
            service="order-service", start=start, end=end
        )
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_result_count_max_100(self, toolkit) -> None:
        result = await toolkit.search_logs(
            service="order-service", keyword="test", limit=200
        )
        assert result.ok
        assert result.metadata["limit"] <= 100

    @pytest.mark.asyncio
    async def test_text_length_max_16kib(self, toolkit) -> None:
        long_keyword = "x" * 17000  # > 16 KiB
        result = await toolkit.search_logs(
            service="order-service", keyword=long_keyword
        )
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_text_length_exactly_16kib_is_ok(self, toolkit) -> None:
        keyword = "x" * 16 * 1024  # exactly 16 KiB
        result = await toolkit.search_logs(
            service="order-service", keyword=keyword
        )
        assert result.ok is True


# ===================================================================
# READ-ONLY — no write operations
# ===================================================================


class TestReadOnly:
    """Tests that tools are read-only."""

    @pytest.mark.asyncio
    async def test_all_tools_have_read_only_permission(self, toolkit) -> None:
        tools = [
            toolkit._query_metrics_tool,
            toolkit._search_logs_tool,
            toolkit._get_slow_traces_tool,
            toolkit._get_trace_tool,
            toolkit._get_service_dependencies_tool,
            toolkit._list_recent_deployments_tool,
            toolkit._get_runbook_tool,
        ]
        for tool in tools:
            assert tool._permission == "read_only"

    @pytest.mark.asyncio
    async def test_timeout_is_3_seconds(self, toolkit) -> None:
        tools = [
            toolkit._query_metrics_tool,
            toolkit._search_logs_tool,
            toolkit._get_slow_traces_tool,
            toolkit._get_trace_tool,
            toolkit._get_service_dependencies_tool,
            toolkit._list_recent_deployments_tool,
            toolkit._get_runbook_tool,
        ]
        for tool in tools:
            assert tool._timeout_seconds == 3

    @pytest.mark.asyncio
    async def test_max_retries_is_1(self, toolkit) -> None:
        tools = [
            toolkit._query_metrics_tool,
            toolkit._search_logs_tool,
            toolkit._get_slow_traces_tool,
            toolkit._get_trace_tool,
            toolkit._get_service_dependencies_tool,
            toolkit._list_recent_deployments_tool,
            toolkit._get_runbook_tool,
        ]
        for tool in tools:
            assert tool._max_retries == 1


# ===================================================================
# UNIFIED RETURN — ToolResult[Any] always
# ===================================================================


class TestUnifiedReturn:
    """Tests that all tools return ToolResult[Any]."""

    @pytest.mark.asyncio
    async def test_all_tools_return_tool_result(self, toolkit) -> None:
        repository = toolkit._repository
        repository.record(_log_event())
        repository.record(_metric_event())
        repository.record(_span_event())
        repository.record(_deployment_event())

        results = [
            await toolkit.query_metrics(service="order-service"),
            await toolkit.search_logs(service="order-service", keyword="payment"),
            await toolkit.get_slow_traces(service="order-service", threshold_seconds=60),
            await toolkit.get_trace(trace_id="trace-a"),
            await toolkit.get_service_dependencies(),
            await toolkit.list_recent_deployments(service="order-service"),
            await toolkit.get_runbook(service="order-service"),
        ]
        for result in results:
            assert isinstance(result, ToolResult)


# ===================================================================
# TELEMETRY API — POST /api/telemetry/events
# ===================================================================


class TestTelemetryAPI:
    """Tests for the telemetry receiving API endpoint."""

    @pytest.mark.asyncio
    async def test_receive_telemetry_event(self) -> None:
        from httpx import ASGITransport, AsyncClient
        from incidentlens_control_plane.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/telemetry/events",
                json={
                    "event_type": "log",
                    "service": "order-service",
                    "trace_id": "trace-api-1",
                    "occurred_at": "2025-01-01T00:00:00Z",
                    "payload": {"level": "ERROR", "message": "api test error"},
                },
            )
        assert response.status_code == 201
        assert response.json()["status"] == "recorded"

    @pytest.mark.asyncio
    async def test_healthz(self) -> None:
        from httpx import ASGITransport, AsyncClient
        from incidentlens_control_plane.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_receive_invalid_event_returns_422(self) -> None:
        from httpx import ASGITransport, AsyncClient
        from incidentlens_control_plane.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/telemetry/events",
                json={"event_type": "log"},  # missing required fields
            )
        assert response.status_code == 422
