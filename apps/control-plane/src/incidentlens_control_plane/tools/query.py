"""Seven audited read-only tools for the control plane.

Tools:
  - query_metrics       — filter metric points by service, name, time range
  - search_logs         — filter log rows by service, keyword
  - get_slow_traces     — find traces with slow spans
  - get_trace           — aggregate all spans for a given trace_id
  - get_service_dependencies — derive service dependency graph from spans
  - list_recent_deployments  — list recent deployments for a service
  - get_runbook         — retrieve a runbook for a service
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from incidentlens_contracts.models import ToolResult
from incidentlens_telemetry.repository import TelemetryRepository
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from incidentlens_control_plane.tools.base import AuditStore, ReadOnlyTool

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_TIME_RANGE_HOURS = 24
MAX_RESULT_COUNT = 100
MAX_TEXT_LENGTH_BYTES = 16 * 1024  # 16 KiB


# ---------------------------------------------------------------------------
# Input models with validation
# ---------------------------------------------------------------------------


class QueryMetricsArgs(BaseModel):
    """Arguments for query_metrics tool."""

    service: str
    name: str | None = None
    trace_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    limit: int = Field(default=100)

    @field_validator("limit")
    @classmethod
    def clamp_limit(cls, v: int) -> int:
        return min(v, MAX_RESULT_COUNT)

    def validate_time_range(self) -> str | None:
        """Validate time range is within 24 hours. Returns error message or None."""
        if self.start is not None and self.end is not None:
            delta = self.end - self.start
            if delta > timedelta(hours=MAX_TIME_RANGE_HOURS):
                return f"Time range exceeds maximum of {MAX_TIME_RANGE_HOURS} hours"
        return None


class SearchLogsArgs(BaseModel):
    """Arguments for search_logs tool."""

    service: str
    keyword: str = ""
    trace_id: str | None = None
    level: str | None = None
    limit: int = Field(default=100)

    @field_validator("limit")
    @classmethod
    def clamp_limit(cls, v: int) -> int:
        return min(v, MAX_RESULT_COUNT)

    @field_validator("keyword")
    @classmethod
    def validate_keyword_length(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_TEXT_LENGTH_BYTES:
            raise ValueError(
                f"Text length exceeds maximum of {MAX_TEXT_LENGTH_BYTES} bytes"
            )
        return v


class GetSlowTracesArgs(BaseModel):
    """Arguments for get_slow_traces tool."""

    service: str
    threshold_seconds: float = 5.0
    limit: int = Field(default=100)

    @field_validator("limit")
    @classmethod
    def clamp_limit(cls, v: int) -> int:
        return min(v, MAX_RESULT_COUNT)


class GetTraceArgs(BaseModel):
    """Arguments for get_trace tool."""

    trace_id: str

    @field_validator("trace_id")
    @classmethod
    def validate_trace_id_length(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_TEXT_LENGTH_BYTES:
            raise ValueError(
                f"Text length exceeds maximum of {MAX_TEXT_LENGTH_BYTES} bytes"
            )
        return v


class GetServiceDependenciesArgs(BaseModel):
    """Arguments for get_service_dependencies tool."""

    limit: int = Field(default=100)

    @field_validator("limit")
    @classmethod
    def clamp_limit(cls, v: int) -> int:
        return min(v, MAX_RESULT_COUNT)


class ListRecentDeploymentsArgs(BaseModel):
    """Arguments for list_recent_deployments tool."""

    service: str
    limit: int = Field(default=100)

    @field_validator("limit")
    @classmethod
    def clamp_limit(cls, v: int) -> int:
        return min(v, MAX_RESULT_COUNT)


class GetRunbookArgs(BaseModel):
    """Arguments for get_runbook tool."""

    service: str

    @field_validator("service")
    @classmethod
    def validate_service_length(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_TEXT_LENGTH_BYTES:
            raise ValueError(
                f"Text length exceeds maximum of {MAX_TEXT_LENGTH_BYTES} bytes"
            )
        return v


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


class QueryMetricsTool(ReadOnlyTool):
    """Query metric points by service, name, and time range."""

    _permission = "read_only"
    _timeout_seconds = 3
    _max_retries = 1

    def __init__(self, repository: TelemetryRepository, audit_store: AuditStore) -> None:
        super().__init__(audit_store)
        self._repository = repository

    def _tool_name(self) -> str:
        return "query_metrics"

    async def _execute(self, args: BaseModel) -> ToolResult[Any]:
        assert isinstance(args, QueryMetricsArgs)
        # Validate time range
        time_error = args.validate_time_range()
        if time_error:
            return ToolResult(ok=False, error=time_error, metadata={"limit": args.limit})

        try:
            rows = self._repository.query_metrics(
                service=args.service,
                name=args.name,
                trace_id=args.trace_id,
                start=args.start,
                end=args.end,
                limit=args.limit,
            )
            return ToolResult(
                ok=True,
                data=rows,
                metadata={"limit": args.limit, "count": len(rows)},
            )
        except Exception as exc:
            return ToolResult(ok=False, error=str(exc), metadata={"limit": args.limit})


class SearchLogsTool(ReadOnlyTool):
    """Search log rows by service and keyword."""

    _permission = "read_only"
    _timeout_seconds = 3
    _max_retries = 1

    def __init__(self, repository: TelemetryRepository, audit_store: AuditStore) -> None:
        super().__init__(audit_store)
        self._repository = repository

    def _tool_name(self) -> str:
        return "search_logs"

    async def _execute(self, args: BaseModel) -> ToolResult[Any]:
        assert isinstance(args, SearchLogsArgs)
        try:
            # First get logs from repository, then filter by keyword
            rows = self._repository.query_logs(
                service=args.service,
                trace_id=args.trace_id,
                level=args.level,
                limit=1000,  # Get more to allow keyword filtering
            )
            # Filter by keyword if provided
            if args.keyword:
                rows = [r for r in rows if args.keyword in str(r.get("message", ""))]

            # Apply limit
            rows = rows[: args.limit]

            return ToolResult(
                ok=True,
                data=rows,
                metadata={"limit": args.limit, "count": len(rows)},
            )
        except Exception as exc:
            return ToolResult(ok=False, error=str(exc), metadata={"limit": args.limit})


class GetSlowTracesTool(ReadOnlyTool):
    """Find traces with spans that exceed a duration threshold."""

    _permission = "read_only"
    _timeout_seconds = 3
    _max_retries = 1

    def __init__(self, repository: TelemetryRepository, audit_store: AuditStore) -> None:
        super().__init__(audit_store)
        self._repository = repository

    def _tool_name(self) -> str:
        return "get_slow_traces"

    async def _execute(self, args: BaseModel) -> ToolResult[Any]:
        assert isinstance(args, GetSlowTracesArgs)
        try:
            from incidentlens_telemetry.models import SpanRow

            engine = self._repository._engine
            with Session(engine) as session:
                # Get all spans for the service, ordered by trace_id and time
                stmt = (
                    select(SpanRow)
                    .where(SpanRow.service == args.service)
                    .order_by(SpanRow.trace_id, SpanRow.occurred_at)
                )
                all_spans = [row.as_dict() for row in session.scalars(stmt)]

            # Group spans by trace_id
            traces: dict[str, list[dict]] = {}
            for span in all_spans:
                tid = span["trace_id"]
                traces.setdefault(tid, []).append(span)

            # Find traces where any span pair suggests slow duration
            slow_traces = []
            for tid, spans in traces.items():
                if len(spans) >= 2:
                    # Calculate duration from first to last span
                    first_time = spans[0]["occurred_at"]
                    last_time = spans[-1]["occurred_at"]
                    if isinstance(first_time, datetime) and isinstance(last_time, datetime):
                        duration = (last_time - first_time).total_seconds()
                        if duration >= args.threshold_seconds:
                            slow_traces.append({
                                "trace_id": tid,
                                "duration_seconds": duration,
                                "span_count": len(spans),
                            })

            # Apply limit
            slow_traces = slow_traces[: args.limit]

            return ToolResult(
                ok=True,
                data=slow_traces,
                metadata={"limit": args.limit, "count": len(slow_traces)},
            )
        except Exception as exc:
            return ToolResult(ok=False, error=str(exc), metadata={"limit": args.limit})


class GetTraceTool(ReadOnlyTool):
    """Aggregate all spans for a given trace_id."""

    _permission = "read_only"
    _timeout_seconds = 3
    _max_retries = 1

    def __init__(self, repository: TelemetryRepository, audit_store: AuditStore) -> None:
        super().__init__(audit_store)
        self._repository = repository

    def _tool_name(self) -> str:
        return "get_trace"

    async def _execute(self, args: BaseModel) -> ToolResult[Any]:
        assert isinstance(args, GetTraceArgs)
        try:
            trace = self._repository.get_trace(args.trace_id)
            return ToolResult(
                ok=True,
                data=trace,
                metadata={"trace_id": args.trace_id},
            )
        except Exception as exc:
            return ToolResult(ok=False, error=str(exc), metadata={"trace_id": args.trace_id})


class GetServiceDependenciesTool(ReadOnlyTool):
    """Derive service dependency graph from span parent-child relationships."""

    _permission = "read_only"
    _timeout_seconds = 3
    _max_retries = 1

    def __init__(self, repository: TelemetryRepository, audit_store: AuditStore) -> None:
        super().__init__(audit_store)
        self._repository = repository

    def _tool_name(self) -> str:
        return "get_service_dependencies"

    async def _execute(self, args: BaseModel) -> ToolResult[Any]:
        assert isinstance(args, GetServiceDependenciesArgs)
        try:
            from incidentlens_telemetry.models import SpanRow

            engine = self._repository._engine
            with Session(engine) as session:
                stmt = select(SpanRow).order_by(SpanRow.trace_id, SpanRow.occurred_at)
                all_spans = [row.as_dict() for row in session.scalars(stmt)]

            # Group spans by trace_id
            traces: dict[str, list[dict]] = {}
            for span in all_spans:
                tid = span["trace_id"]
                traces.setdefault(tid, []).append(span)

            # Build dependency edges: parent span's service -> child span's service
            # First, index spans by span_id within each trace
            edges: set[tuple[str, str]] = set()
            for tid, spans in traces.items():
                span_index = {s["span_id"]: s for s in spans}
                for span in spans:
                    parent_id = span.get("parent_id")
                    if parent_id and parent_id in span_index:
                        parent_span = span_index[parent_id]
                        parent_service = parent_span["service"]
                        child_service = span["service"]
                        if parent_service != child_service:
                            edges.add((parent_service, child_service))

            # Convert to list of dicts
            dependencies = [
                {"from": src, "to": dst} for src, dst in sorted(edges)
            ][: args.limit]

            return ToolResult(
                ok=True,
                data=dependencies,
                metadata={"limit": args.limit, "count": len(dependencies)},
            )
        except Exception as exc:
            return ToolResult(ok=False, error=str(exc), metadata={"limit": args.limit})


class ListRecentDeploymentsTool(ReadOnlyTool):
    """List recent deployments for a service."""

    _permission = "read_only"
    _timeout_seconds = 3
    _max_retries = 1

    def __init__(self, repository: TelemetryRepository, audit_store: AuditStore) -> None:
        super().__init__(audit_store)
        self._repository = repository

    def _tool_name(self) -> str:
        return "list_recent_deployments"

    async def _execute(self, args: BaseModel) -> ToolResult[Any]:
        assert isinstance(args, ListRecentDeploymentsArgs)
        try:
            rows = self._repository.query_deployments(
                service=args.service,
                limit=args.limit,
            )
            return ToolResult(
                ok=True,
                data=rows,
                metadata={"limit": args.limit, "count": len(rows)},
            )
        except Exception as exc:
            return ToolResult(ok=False, error=str(exc), metadata={"limit": args.limit})


# ---------------------------------------------------------------------------
# Runbook data (static, in-memory for now)
# ---------------------------------------------------------------------------

_RUNBOOKS: dict[str, dict[str, Any]] = {
    "order-service": {
        "service": "order-service",
        "runbook": "Order Service Runbook",
        "common_issues": [
            "High latency on checkout",
            "Payment service timeouts",
            "Database connection pool exhaustion",
        ],
        "escalation": "platform-team@example.com",
    },
    "payment-service": {
        "service": "payment-service",
        "runbook": "Payment Service Runbook",
        "common_issues": [
            "Charge processing delays",
            "Third-party gateway timeouts",
            "Rate limiting from payment provider",
        ],
        "escalation": "payments-team@example.com",
    },
    "gateway-service": {
        "service": "gateway-service",
        "runbook": "Gateway Service Runbook",
        "common_issues": [
            "Upstream service unavailable",
            "Request routing errors",
            "TLS certificate expiry",
        ],
        "escalation": "platform-team@example.com",
    },
}


class GetRunbookTool(ReadOnlyTool):
    """Retrieve a runbook for a service."""

    _permission = "read_only"
    _timeout_seconds = 3
    _max_retries = 1

    def __init__(self, audit_store: AuditStore) -> None:
        super().__init__(audit_store)

    def _tool_name(self) -> str:
        return "get_runbook"

    async def _execute(self, args: BaseModel) -> ToolResult[Any]:
        assert isinstance(args, GetRunbookArgs)
        runbook = _RUNBOOKS.get(args.service)
        return ToolResult(
            ok=True,
            data=runbook,
            metadata={"service": args.service},
        )


# ---------------------------------------------------------------------------
# Toolkit — convenience wrapper for all 7 tools
# ---------------------------------------------------------------------------


class ReadOnlyToolkit:
    """Convenience wrapper that exposes all 7 read-only tools as methods.

    Each method creates the appropriate args model, validates it, and
    delegates to the corresponding tool's invoke() method.
    """

    def __init__(self, repository: TelemetryRepository) -> None:
        self._repository = repository
        engine = repository._engine
        self._audit_store = AuditStore(engine)

        self._query_metrics_tool = QueryMetricsTool(repository, self._audit_store)
        self._search_logs_tool = SearchLogsTool(repository, self._audit_store)
        self._get_slow_traces_tool = GetSlowTracesTool(repository, self._audit_store)
        self._get_trace_tool = GetTraceTool(repository, self._audit_store)
        self._get_service_dependencies_tool = GetServiceDependenciesTool(
            repository, self._audit_store
        )
        self._list_recent_deployments_tool = ListRecentDeploymentsTool(
            repository, self._audit_store
        )
        self._get_runbook_tool = GetRunbookTool(self._audit_store)

    # ------------------------------------------------------------------
    # Convenience methods that create args and invoke
    # ------------------------------------------------------------------

    async def query_metrics(
        self,
        *,
        service: str,
        name: str | None = None,
        trace_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> ToolResult[Any]:
        args = QueryMetricsArgs(
            service=service,
            name=name,
            trace_id=trace_id,
            start=start,
            end=end,
            limit=limit,
        )
        # Validate time range before invoking
        time_error = args.validate_time_range()
        if time_error:
            # Still audit the failed invocation
            self._audit_store.record(
                tool_name="query_metrics",
                parameters=args.model_dump_json(),
                result_summary=f"error: {time_error}",
                duration_ms=0.0,
                retries=0,
                error=time_error,
            )
            return ToolResult(ok=False, error=time_error, metadata={"limit": args.limit})

        return await self._query_metrics_tool.invoke(args)

    async def search_logs(
        self,
        *,
        service: str,
        keyword: str = "",
        trace_id: str | None = None,
        level: str | None = None,
        limit: int = 100,
    ) -> ToolResult[Any]:
        try:
            args = SearchLogsArgs(
                service=service,
                keyword=keyword,
                trace_id=trace_id,
                level=level,
                limit=limit,
            )
        except Exception as exc:
            # Validation error — still audit and return ToolResult
            self._audit_store.record(
                tool_name="search_logs",
                parameters=json.dumps({"service": service, "keyword": keyword[:100]}),
                result_summary=f"error: {exc}",
                duration_ms=0.0,
                retries=0,
                error=str(exc),
            )
            return ToolResult(
                ok=False, error=str(exc), metadata={"limit": min(limit, MAX_RESULT_COUNT)}
            )

        return await self._search_logs_tool.invoke(args)

    async def get_slow_traces(
        self,
        *,
        service: str,
        threshold_seconds: float = 5.0,
        limit: int = 100,
    ) -> ToolResult[Any]:
        args = GetSlowTracesArgs(
            service=service,
            threshold_seconds=threshold_seconds,
            limit=limit,
        )
        return await self._get_slow_traces_tool.invoke(args)

    async def get_trace(
        self,
        *,
        trace_id: str,
    ) -> ToolResult[Any]:
        try:
            args = GetTraceArgs(trace_id=trace_id)
        except Exception as exc:
            self._audit_store.record(
                tool_name="get_trace",
                parameters=json.dumps({"trace_id": trace_id[:100]}),
                result_summary=f"error: {exc}",
                duration_ms=0.0,
                retries=0,
                error=str(exc),
            )
            return ToolResult(ok=False, error=str(exc))

        return await self._get_trace_tool.invoke(args)

    async def get_service_dependencies(
        self,
        *,
        limit: int = 100,
    ) -> ToolResult[Any]:
        args = GetServiceDependenciesArgs(limit=limit)
        return await self._get_service_dependencies_tool.invoke(args)

    async def list_recent_deployments(
        self,
        *,
        service: str,
        limit: int = 100,
    ) -> ToolResult[Any]:
        args = ListRecentDeploymentsArgs(service=service, limit=limit)
        return await self._list_recent_deployments_tool.invoke(args)

    async def get_runbook(
        self,
        *,
        service: str,
    ) -> ToolResult[Any]:
        try:
            args = GetRunbookArgs(service=service)
        except Exception as exc:
            self._audit_store.record(
                tool_name="get_runbook",
                parameters=json.dumps({"service": service[:100]}),
                result_summary=f"error: {exc}",
                duration_ms=0.0,
                retries=0,
                error=str(exc),
            )
            return ToolResult(ok=False, error=str(exc))

        return await self._get_runbook_tool.invoke(args)
