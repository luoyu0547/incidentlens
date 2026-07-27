"""Tests for the telemetry repository — TDD RED phase.

These tests define the desired interface for TelemetryRepository:
  - record(event)          — persist a TelemetryEvent
  - query_logs(...)        — filter log rows by service, trace_id, level
  - query_metrics(...)     — filter metric points by service, name, time range
  - get_trace(trace_id)    — aggregate all spans for a given trace_id
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from incidentlens_contracts.models import TelemetryEvent

# ---------------------------------------------------------------------------
# Fixture: in-memory SQLite session + repository
# ---------------------------------------------------------------------------


@pytest.fixture()
def repository():
    """Create a TelemetryRepository backed by an in-memory SQLite DB."""
    from incidentlens_telemetry.database import create_engine
    from incidentlens_telemetry.repository import TelemetryRepository

    engine = create_engine("sqlite:///:memory:")
    return TelemetryRepository(engine)


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
# LOG TESTS
# ===================================================================


class TestQueryLogs:
    """Tests for repository.query_logs()."""

    def test_query_logs_filters_service_and_trace(self, repository) -> None:
        repository.record(_log_event(service="order-service", trace_id="trace-a"))
        repository.record(_log_event(service="payment-service", trace_id="trace-a"))
        rows = repository.query_logs(service="order-service", trace_id="trace-a")
        assert len(rows) == 1
        assert rows[0]["message"] == "payment failed"

    def test_query_logs_filters_by_service_only(self, repository) -> None:
        repository.record(_log_event(service="order-service", trace_id="trace-a"))
        repository.record(_log_event(service="order-service", trace_id="trace-b"))
        repository.record(_log_event(service="payment-service", trace_id="trace-a"))
        rows = repository.query_logs(service="order-service")
        assert len(rows) == 2

    def test_query_logs_filters_by_level(self, repository) -> None:
        repository.record(_log_event(level="ERROR", message="err"))
        repository.record(_log_event(level="INFO", message="info"))
        rows = repository.query_logs(service="order-service", level="ERROR")
        assert len(rows) == 1
        assert rows[0]["message"] == "err"

    def test_query_logs_respects_limit(self, repository) -> None:
        for i in range(5):
            repository.record(_log_event(message=f"msg-{i}"))
        rows = repository.query_logs(service="order-service", limit=3)
        assert len(rows) == 3

    def test_query_logs_returns_empty_when_no_match(self, repository) -> None:
        repository.record(_log_event(service="order-service"))
        rows = repository.query_logs(service="unknown-service")
        assert rows == []


# ===================================================================
# METRIC TESTS
# ===================================================================


class TestQueryMetrics:
    """Tests for repository.query_metrics()."""

    def test_query_metrics_filters_service_and_name(self, repository) -> None:
        repository.record(_metric_event(name="request_duration_ms", value=120.5))
        repository.record(_metric_event(name="error_rate", value=0.01))
        rows = repository.query_metrics(service="order-service", name="request_duration_ms")
        assert len(rows) == 1
        assert rows[0]["value"] == 120.5

    def test_query_metrics_filters_by_time_range(self, repository) -> None:
        repository.record(_metric_event(minute=10, name="cpu_pct", value=80.0))
        repository.record(_metric_event(minute=30, name="cpu_pct", value=90.0))
        repository.record(_metric_event(minute=50, name="cpu_pct", value=70.0))
        rows = repository.query_metrics(
            service="order-service",
            name="cpu_pct",
            start=_ts(20),
            end=_ts(40),
        )
        assert len(rows) == 1
        assert rows[0]["value"] == 90.0

    def test_query_metrics_returns_all_for_service(self, repository) -> None:
        repository.record(_metric_event(name="cpu_pct", value=80.0))
        repository.record(_metric_event(name="mem_pct", value=60.0))
        rows = repository.query_metrics(service="order-service")
        assert len(rows) == 2

    def test_query_metrics_respects_limit(self, repository) -> None:
        for i in range(5):
            repository.record(_metric_event(name="cpu_pct", value=float(i)))
        rows = repository.query_metrics(service="order-service", limit=3)
        assert len(rows) == 3


# ===================================================================
# TRACE TESTS
# ===================================================================


class TestGetTrace:
    def test_get_trace_aggregates_spans(self, repository) -> None:
        repository.record(_span_event(span_id="span-1", operation="POST /checkout"))
        repository.record(_span_event(span_id="span-2", parent_id="span-1", operation="DB insert"))
        trace = repository.get_trace("trace-a")
        assert trace is not None
        assert len(trace["spans"]) == 2
        # Spans should be ordered by occurred_at
        assert trace["spans"][0]["span_id"] == "span-1"
        assert trace["spans"][1]["span_id"] == "span-2"

    def test_get_trace_returns_none_when_not_found(self, repository) -> None:
        trace = repository.get_trace("nonexistent")
        assert trace is None

    def test_get_trace_includes_service_info(self, repository) -> None:
        repository.record(_span_event(service="order-service"))
        trace = repository.get_trace("trace-a")
        assert trace is not None
        assert trace["spans"][0]["service"] == "order-service"


# ===================================================================
# DEPLOYMENT TESTS
# ===================================================================


class TestDeployments:
    """Tests for deployment recording and querying."""

    def test_record_deployment(self, repository) -> None:
        repository.record(_deployment_event(service="order-service", version="v2.3.1"))
        rows = repository.query_deployments(service="order-service")
        assert len(rows) == 1
        assert rows[0]["version"] == "v2.3.1"

    def test_query_deployments_filters_by_service(self, repository) -> None:
        repository.record(_deployment_event(service="order-service", version="v2.3.1"))
        repository.record(_deployment_event(service="payment-service", version="v1.0.0"))
        rows = repository.query_deployments(service="order-service")
        assert len(rows) == 1
        assert rows[0]["version"] == "v2.3.1"

    def test_query_deployments_respects_limit(self, repository) -> None:
        for i in range(5):
            repository.record(_deployment_event(service="order-service", version=f"v1.{i}"))
        rows = repository.query_deployments(service="order-service", limit=3)
        assert len(rows) == 3


# ===================================================================
# RECORD TESTS
# ===================================================================


class TestRecord:
    """Tests for repository.record() with TelemetryEvent."""

    def test_record_log_event(self, repository) -> None:
        event = _log_event()
        repository.record(event)
        rows = repository.query_logs(service="order-service")
        assert len(rows) == 1
        assert rows[0]["message"] == "payment failed"
        assert rows[0]["level"] == "ERROR"

    def test_record_metric_event(self, repository) -> None:
        event = _metric_event()
        repository.record(event)
        rows = repository.query_metrics(service="order-service")
        assert len(rows) == 1

    def test_record_span_event(self, repository) -> None:
        event = _span_event()
        repository.record(event)
        trace = repository.get_trace("trace-a")
        assert trace is not None
        assert len(trace["spans"]) == 1

    def test_record_deployment_event(self, repository) -> None:
        event = _deployment_event()
        repository.record(event)
        rows = repository.query_deployments(service="order-service")
        assert len(rows) == 1

    def test_record_unknown_event_type_is_ignored(self, repository) -> None:
        event = TelemetryEvent(
            event_type="unknown",
            service="order-service",
            trace_id="trace-a",
            occurred_at=_ts(0),
            payload={},
        )
        # Should not raise — unknown types are silently ignored
        repository.record(event)
