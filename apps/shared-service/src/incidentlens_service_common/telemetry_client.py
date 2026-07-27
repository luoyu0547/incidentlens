"""Telemetry client for emitting events from services.

Each service emits JSON log, metric, and span events structured
as TelemetryEvent instances. In the current phase, events are
logged to stdout. The control plane API (Task 4) will consume them.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from incidentlens_contracts.models import TelemetryEvent

logger = logging.getLogger("incidentlens.telemetry")


class TelemetryClient:
    """Client for emitting telemetry events from a service.

    Events are structured as TelemetryEvent instances and logged
    as JSON to stdout. The control plane API will consume them later.
    """

    def __init__(self, service_name: str) -> None:
        self._service = service_name

    def emit_log(
        self,
        trace_id: str,
        level: str,
        message: str,
        **extra: Any,
    ) -> TelemetryEvent:
        """Emit a log event."""
        payload: dict[str, Any] = {"level": level, "message": message}
        payload.update(extra)
        event = TelemetryEvent(
            event_type="log",
            service=self._service,
            trace_id=trace_id,
            occurred_at=datetime.now(tz=timezone.utc),
            payload=payload,
        )
        self._log_event(event)
        return event

    def emit_metric(
        self,
        trace_id: str,
        name: str,
        value: float,
        **extra: Any,
    ) -> TelemetryEvent:
        """Emit a metric event."""
        payload: dict[str, Any] = {"name": name, "value": value}
        payload.update(extra)
        event = TelemetryEvent(
            event_type="metric",
            service=self._service,
            trace_id=trace_id,
            occurred_at=datetime.now(tz=timezone.utc),
            payload=payload,
        )
        self._log_event(event)
        return event

    def emit_span(
        self,
        trace_id: str,
        span_id: str,
        operation: str,
        parent_id: str | None = None,
        **extra: Any,
    ) -> TelemetryEvent:
        """Emit a span event."""
        payload: dict[str, Any] = {
            "span_id": span_id,
            "operation": operation,
        }
        if parent_id is not None:
            payload["parent_id"] = parent_id
        payload.update(extra)
        event = TelemetryEvent(
            event_type="span",
            service=self._service,
            trace_id=trace_id,
            occurred_at=datetime.now(tz=timezone.utc),
            payload=payload,
        )
        self._log_event(event)
        return event

    def _log_event(self, event: TelemetryEvent) -> None:
        """Log the event as JSON to stdout."""
        logger.info(json.dumps(event.model_dump(mode="json")))
