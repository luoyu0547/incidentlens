"""SSE event models and in-process event bus.

Event types:
  - state_changed: investigation status or phase changed
  - tool_called: a read-only tool was invoked
  - evidence_recorded: new evidence was recorded
  - report_ready: investigation report is available

The EventBus allows publishing events and subscribing to them via async iterators,
which the SSE endpoint consumes to stream events to clients.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from typing import Any, AsyncIterator

from pydantic import BaseModel


def _json_default(obj: Any) -> Any:
    """Fallback JSON serializer for non-standard types like datetime."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class SSEEvent(BaseModel):
    """Server-Sent Event payload.

    Attributes:
        event_type: one of state_changed, tool_called, evidence_recorded, report_ready
        data: arbitrary dict payload for the event
    """

    event_type: str
    data: dict[str, Any] = {}

    def to_sse_message(self) -> str:
        """Format as SSE message: event: type\\ndata: json\\n\\n"""
        return f"event: {self.event_type}\ndata: {json.dumps(self.data, default=_json_default)}\n\n"


class EventBus:
    """In-process pub/sub for investigation SSE events.

    Each incident_id gets a list of asyncio.Queue subscribers.
    Publishers call publish() to send events to all subscribers.
    The SSE endpoint calls subscribe() to get an async iterator.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[str]]] = defaultdict(list)

    def publish(self, incident_id: str, event: SSEEvent) -> None:
        """Publish an event to all subscribers for the given incident."""
        message = event.to_sse_message()
        for queue in self._subscribers.get(incident_id, []):
            queue.put_nowait(message)

    def subscribe(self, incident_id: str) -> AsyncIterator[str]:
        """Subscribe to events for the given incident_id.

        Returns an async iterator that yields SSE-formatted strings.
        """
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers[incident_id].append(queue)
        return _queue_iterator(queue)

    def unsubscribe(self, incident_id: str, iterator: AsyncIterator[str]) -> None:
        """Remove a subscriber's queue from the event bus."""
        # The iterator wraps a queue; find and remove it
        if incident_id in self._subscribers:
            # Get the queue from the iterator for comparison
            target_queue = getattr(iterator, '_queue', None)
            if target_queue is not None:
                self._subscribers[incident_id] = [
                    q for q in self._subscribers[incident_id] if q is not target_queue
                ]
            else:
                # Best-effort: clear all subscribers for this incident
                self._subscribers[incident_id] = []
            if not self._subscribers[incident_id]:
                del self._subscribers[incident_id]


async def _queue_iterator(queue: asyncio.Queue[str]) -> AsyncIterator[str]:
    """Async iterator that yields items from an asyncio.Queue."""
    while True:
        item = await queue.get()
        yield item
        queue.task_done()


# Global event bus instance — shared across the application
_global_bus = EventBus()
