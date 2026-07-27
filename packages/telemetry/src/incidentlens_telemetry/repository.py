"""TelemetryRepository — persist and query telemetry data.

Public interface:
  - record(event)           — persist a TelemetryEvent to the right table
  - query_logs(...)         — filter log rows
  - query_metrics(...)      — filter metric points
  - get_trace(trace_id)     — aggregate spans for a trace
  - query_deployments(...)  — filter deployment records
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from incidentlens_contracts.models import TelemetryEvent
from sqlalchemy import Engine, Select, select
from sqlalchemy.orm import Session

from incidentlens_telemetry.models import (
    Base,
    DeploymentRow,
    LogRow,
    MetricRow,
    SpanRow,
)


class TelemetryRepository:
    """Repository for recording and querying telemetry data via SQLAlchemy."""

    def __init__(self, engine: Engine) -> None:
        # Tables are created by create_engine(); no need to call create_all here.
        self._engine = engine

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def record(self, event: TelemetryEvent) -> None:
        """Persist a TelemetryEvent to the appropriate table.

        Unknown event_type values are silently ignored.
        """
        with Session(self._engine) as session:
            row = self._event_to_row(event)
            if row is not None:
                session.add(row)
                session.commit()

    # ------------------------------------------------------------------
    # Read path — logs
    # ------------------------------------------------------------------

    def query_logs(
        self,
        *,
        service: str,
        trace_id: str | None = None,
        level: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return log rows matching the given filters."""
        limit = min(limit, 1000)
        with Session(self._engine) as session:
            stmt: Select = select(LogRow).where(LogRow.service == service)
            if trace_id is not None:
                stmt = stmt.where(LogRow.trace_id == trace_id)
            if level is not None:
                stmt = stmt.where(LogRow.level == level)
            stmt = stmt.order_by(LogRow.occurred_at).limit(limit)
            return [row.as_dict() for row in session.scalars(stmt)]

    # ------------------------------------------------------------------
    # Read path — metrics
    # ------------------------------------------------------------------

    def query_metrics(
        self,
        *,
        service: str,
        trace_id: str | None = None,
        name: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return metric points matching the given filters."""
        limit = min(limit, 1000)
        with Session(self._engine) as session:
            stmt: Select = select(MetricRow).where(MetricRow.service == service)
            if trace_id is not None:
                stmt = stmt.where(MetricRow.trace_id == trace_id)
            if name is not None:
                stmt = stmt.where(MetricRow.name == name)
            if start is not None:
                stmt = stmt.where(MetricRow.occurred_at >= start)
            if end is not None:
                stmt = stmt.where(MetricRow.occurred_at <= end)
            stmt = stmt.order_by(MetricRow.occurred_at).limit(limit)
            return [row.as_dict() for row in session.scalars(stmt)]

    # ------------------------------------------------------------------
    # Read path — traces
    # ------------------------------------------------------------------

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Aggregate all spans for a given trace_id.

        Returns ``None`` if no spans exist for the trace.
        """
        with Session(self._engine) as session:
            stmt = select(SpanRow).where(SpanRow.trace_id == trace_id).order_by(SpanRow.occurred_at)
            spans = [row.as_dict() for row in session.scalars(stmt)]
            if not spans:
                return None
            return {"trace_id": trace_id, "spans": spans}

    # ------------------------------------------------------------------
    # Read path — deployments
    # ------------------------------------------------------------------

    def query_deployments(
        self,
        *,
        service: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return deployment records for a service, newest first."""
        limit = min(limit, 1000)
        with Session(self._engine) as session:
            stmt = (
                select(DeploymentRow)
                .where(DeploymentRow.service == service)
                .order_by(DeploymentRow.occurred_at.desc())
                .limit(limit)
            )
            return [row.as_dict() for row in session.scalars(stmt)]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _event_to_row(event: TelemetryEvent) -> Base | None:
        """Convert a TelemetryEvent to the appropriate ORM row.

        Returns None for unknown event types.
        """
        payload = event.payload
        ts = event.occurred_at

        if event.event_type == "log":
            return LogRow(
                service=event.service,
                trace_id=event.trace_id,
                level=payload.get("level", "INFO"),
                message=payload.get("message", ""),
                occurred_at=ts,
            )
        if event.event_type == "metric":
            return MetricRow(
                service=event.service,
                trace_id=event.trace_id,
                name=payload.get("name", ""),
                value=float(payload.get("value", 0.0)),
                occurred_at=ts,
            )
        if event.event_type == "span":
            return SpanRow(
                service=event.service,
                trace_id=event.trace_id,
                span_id=payload.get("span_id", ""),
                parent_id=payload.get("parent_id"),
                operation=payload.get("operation", ""),
                occurred_at=ts,
            )
        if event.event_type == "deployment":
            return DeploymentRow(
                service=event.service,
                version=payload.get("version", ""),
                occurred_at=ts,
            )
        # Unknown event type — silently ignore
        return None
