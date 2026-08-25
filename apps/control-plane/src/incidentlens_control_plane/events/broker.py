import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from incidentlens_control_plane.events.types import RuntimeEvent


class RuntimeEventBroker:
    def __init__(self, queue_size: int = 100) -> None:
        self._queue_size = queue_size
        self._subscribers: list[asyncio.Queue[RuntimeEvent]] = []
        self._overflow: dict[asyncio.Queue[RuntimeEvent], int] = {}

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[RuntimeEvent]]:
        """Subscribe to events. Yields a bounded queue of events."""
        queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.append(queue)
        self._overflow[queue] = 0
        try:
            yield queue
        finally:
            self._subscribers.remove(queue)
            self._overflow.pop(queue, None)

    def dropped_count(self, queue: asyncio.Queue[RuntimeEvent]) -> int:
        """Return how many frames were dropped for *queue* due to overflow."""
        return self._overflow.get(queue, 0)

    def max_size(self) -> int:
        return self._queue_size

    async def publish(self, event: RuntimeEvent) -> None:
        """Publish an event to all active subscribers. Never blocks."""
        for queue in self._subscribers:
            if queue.full():
                # Evict the oldest frame, but record the overflow so the stream
                # layer can signal a slow consumer instead of dropping silently.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._overflow[queue] = self._overflow.get(queue, 0) + 1
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # This should not happen after removing oldest item, but handle it
                pass
