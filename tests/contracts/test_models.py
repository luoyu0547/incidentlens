"""Contract tests for incidentlens_contracts models."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from incidentlens_contracts.models import (
    Evidence,
    Hypothesis,
    HypothesisStatus,
    InvestigationStatus,
    TelemetryEvent,
    ToolResult,
)


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


# --- TelemetryEvent ---


def test_telemetry_event_requires_trace_and_service() -> None:
    ts = now_utc()
    event = TelemetryEvent(
        event_type="log",
        service="order-service",
        trace_id="trace-1",
        occurred_at=ts,
        payload={"message": "created"},
    )
    assert event.trace_id == "trace-1"
    assert event.occurred_at == ts


def test_telemetry_event_rejects_missing_trace_id() -> None:
    with pytest.raises(ValidationError):
        TelemetryEvent(
            event_type="log",
            service="order-service",
            occurred_at=now_utc(),
            payload={"message": "created"},
        )


def test_telemetry_event_rejects_missing_service() -> None:
    with pytest.raises(ValidationError):
        TelemetryEvent(
            event_type="log",
            trace_id="trace-1",
            occurred_at=now_utc(),
            payload={"message": "created"},
        )


# --- ToolResult ---


def test_tool_result_ok_with_data() -> None:
    result: ToolResult[list[str]] = ToolResult(
        ok=True, data=["log-1", "log-2"], metadata={"limit": 100}
    )
    assert result.ok is True
    assert result.data == ["log-1", "log-2"]
    assert result.error is None


def test_tool_result_failure_with_error() -> None:
    result: ToolResult[None] = ToolResult(ok=False, error="timeout")
    assert result.ok is False
    assert result.data is None
    assert result.error == "timeout"


def test_tool_result_default_metadata() -> None:
    result = ToolResult(ok=True, data=42)
    assert result.metadata == {}


# --- Evidence ---


def test_evidence_creation() -> None:
    ev = Evidence(
        id="ev-1",
        source_tool="search_logs",
        tool_call_id="call-1",
        content={"message": "timeout at payment"},
        supports_hypothesis_ids=["h-1"],
        contradicts_hypothesis_ids=[],
    )
    assert ev.id == "ev-1"
    assert ev.source_tool == "search_logs"


# --- Hypothesis ---


def test_hypothesis_creation() -> None:
    h = Hypothesis(
        id="h-1",
        description="Payment service is slow due to injected delay",
        confidence=0.6,
        supporting_evidence_ids=["ev-1"],
        contradicting_evidence_ids=[],
        status=HypothesisStatus.ACTIVE,
    )
    assert h.id == "h-1"
    assert h.confidence == 0.6
    assert h.status == "active"


def test_hypothesis_default_status_is_active() -> None:
    h = Hypothesis(
        id="h-2",
        description="Some hypothesis",
    )
    assert h.status == HypothesisStatus.ACTIVE


# --- InvestigationStatus ---


def test_investigation_status_values() -> None:
    assert InvestigationStatus.SCOPING == "scoping"
    assert InvestigationStatus.INVESTIGATING == "investigating"
    assert InvestigationStatus.VERIFYING == "verifying"
    assert InvestigationStatus.REPORT_READY == "report_ready"
    assert InvestigationStatus.NEEDS_MORE_EVIDENCE == "needs_more_evidence"


# --- HypothesisStatus ---


def test_hypothesis_status_values() -> None:
    assert HypothesisStatus.ACTIVE == "active"
    assert HypothesisStatus.RULED_OUT == "ruled_out"
    assert HypothesisStatus.CONFIRMED == "confirmed"
