import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from incidentlens_control_plane.events.types import RuntimeEvent


class RuntimeEventBroker:
    def __init__(self, queue_size: int = 100) -> None:
        self._queue_size = queue_size
        self._subscribers: list[asyncio.Queue[RuntimeEvent]] = []

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[RuntimeEvent]]:
        """Subscribe to events. Yields a bounded queue of events."""
        queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.append(queue)
        try:
            yield queue
        finally:
            self._subscribers.remove(queue)

    async def publish(self, event: RuntimeEvent) -> None:
        """Publish an event to all active subscribers. Never blocks."""
        for queue in self._subscribers:
            if queue.full():
                # Remove oldest item before adding new event
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # This should not happen after removing oldest item, but handle it
                pass
