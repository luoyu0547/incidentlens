"""Telemetry client for emitting events from services.

Each service emits JSON log, metric, and span events structured
as TelemetryEvent instances. Events are logged to stdout AND
posted to the control plane's /api/telemetry/events endpoint
when a control_plane_url is configured.

HTTP delivery is fire-and-forget: emit methods schedule an async
POST via asyncio.ensure_future so that callers never need to
await telemetry delivery explicitly. The _post_event method can
also be awaited directly when the caller wants to ensure delivery
(e.g., on error paths where the response is about to be sent).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from incidentlens_contracts.models import TelemetryEvent

logger = logging.getLogger("incidentlens.telemetry")


class TelemetryClient:
    """Client for emitting telemetry events from a service.

    Events are structured as TelemetryEvent instances and:
      1. Logged as JSON to stdout (local diagnostic)
      2. POSTed to the control plane's /api/telemetry/events endpoint
         when control_plane_url is configured.

    HTTP delivery uses a short timeout and catches all httpx.HTTPError
    exceptions so that telemetry failures never crash the service.
    The emit methods schedule the POST via asyncio.ensure_future
    (fire-and-forget), but _post_event can also be awaited directly.
    """

    def __init__(
        self,
        service_name: str,
        control_plane_url: str | None = None,
        http_timeout: float = 2.0,
    ) -> None:
        self._service = service_name
        self._control_plane_url = control_plane_url.rstrip("/") if control_plane_url else None
        self._http_timeout = http_timeout
        self._client: httpx.AsyncClient | None = None

    def _get_http_client(self) -> httpx.AsyncClient | None:
        """Get or create the async HTTP client for posting to the control plane."""
        if self._control_plane_url is None:
            return None
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._control_plane_url,
                timeout=self._http_timeout,
            )
        return self._client

    async def _post_event(self, event: TelemetryEvent) -> None:
        """POST a telemetry event to the control plane.

        Catches all httpx.HTTPError exceptions so that telemetry
        delivery failures never crash the service. Logs a warning
        on failure for diagnostics.
        """
        client = self._get_http_client()
        if client is None:
            return
        try:
            response = await client.post(
                "/api/telemetry/events",
                json=event.model_dump(mode="json"),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "telemetry delivery failed: %s",
                str(exc),
                extra={"service": self._service, "event_type": event.event_type},
            )

    def _schedule_post(self, event: TelemetryEvent) -> None:
        """Schedule an async POST to the control plane (fire-and-forget)."""
        if self._control_plane_url is None:
            return
        try:
            asyncio.ensure_future(self._post_event(event))
        except RuntimeError:
            # No event loop running (e.g., during import or sync context)
            pass

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
        self._schedule_post(event)
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
        self._schedule_post(event)
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
        self._schedule_post(event)
        return event

    def _log_event(self, event: TelemetryEvent) -> None:
        """Log the event as JSON to stdout."""
        logger.info(json.dumps(event.model_dump(mode="json")))
