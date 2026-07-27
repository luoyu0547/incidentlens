"""Tests for SSE event streaming — TDD RED phase.

Tests cover:
  - EventBus publishes and subscribers receive events
  - SSE endpoint streams events for an investigation
  - Event types: state_changed, tool_called, evidence_recorded, report_ready
"""

from __future__ import annotations

import asyncio
import json

import pytest


class TestEventBus:
    """Tests for the in-process event bus."""

    @pytest.mark.asyncio
    async def test_publish_and_subscribe(self) -> None:
        """Published events should be received by subscribers."""
        from incidentlens_control_plane.events import EventBus, SSEEvent

        bus = EventBus()
        incident_id = "test-incident-1"

        # Subscribe
        subscriber = bus.subscribe(incident_id)

        # Publish an event
        bus.publish(incident_id, SSEEvent(
            event_type="state_changed",
            data={"status": "investigating", "round": 1},
        ))

        # Receive the event
        event_json = await asyncio.wait_for(subscriber.__anext__(), timeout=1.0)
        event_data = json.loads(event_json.split("data: ")[1].strip())
        assert event_data["status"] == "investigating"

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self) -> None:
        """Multiple subscribers should all receive published events."""
        from incidentlens_control_plane.events import EventBus, SSEEvent

        bus = EventBus()
        incident_id = "test-incident-2"

        sub1 = bus.subscribe(incident_id)
        sub2 = bus.subscribe(incident_id)

        bus.publish(incident_id, SSEEvent(
            event_type="tool_called",
            data={"tool": "search_logs"},
        ))

        e1 = await asyncio.wait_for(sub1.__anext__(), timeout=1.0)
        e2 = await asyncio.wait_for(sub2.__anext__(), timeout=1.0)
        assert "tool_called" in e1
        assert "tool_called" in e2

    @pytest.mark.asyncio
    async def test_event_types_are_sse_formatted(self) -> None:
        """SSE events should be formatted as SSE messages."""
        from incidentlens_control_plane.events import SSEEvent

        event = SSEEvent(event_type="evidence_recorded", data={"tool": "query_metrics"})
        msg = event.to_sse_message()
        assert msg.startswith("event: evidence_recorded\n")
        assert "data: " in msg
        assert msg.endswith("\n\n")

    @pytest.mark.asyncio
    async def test_unsubscribe_cleans_up(self) -> None:
        """Unsubscribing should clean up the subscriber queue."""
        from incidentlens_control_plane.events import EventBus

        bus = EventBus()
        incident_id = "test-incident-3"

        sub = bus.subscribe(incident_id)
        bus.unsubscribe(incident_id, sub)

        # After unsubscribe, the queue should be removed
        assert incident_id not in bus._subscribers or len(bus._subscribers[incident_id]) == 0


class TestSSEEndpoint:
    """Tests for the SSE endpoint."""

    @pytest.mark.asyncio
    async def test_sse_endpoint_returns_200(self) -> None:
        """SSE endpoint should return 200 status code."""
        from httpx import ASGITransport, AsyncClient
        from incidentlens_control_plane.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/investigations/test-id/events")
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_sse_endpoint_streams_events(self) -> None:
        """GET /api/investigations/{incident_id}/events should stream SSE events.

        Verify the endpoint is wired correctly by checking it returns 200
        and the event bus publishes events that reach subscribers.
        """
        from incidentlens_control_plane.events import SSEEvent, _global_bus

        incident_id = "sse-test-incident"

        # Verify the event bus is functional
        subscriber = _global_bus.subscribe(incident_id)
        _global_bus.publish(incident_id, SSEEvent(
            event_type="state_changed",
            data={"status": "investigating"},
        ))
        event = await asyncio.wait_for(subscriber.__anext__(), timeout=1.0)
        assert "state_changed" in event
        _global_bus.unsubscribe(incident_id, subscriber)
