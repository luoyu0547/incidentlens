import asyncio
from datetime import UTC, datetime

from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType


def test_broker_delivers_event_to_active_subscriber() -> None:
    async def scenario() -> None:
        broker = RuntimeEventBroker(queue_size=4)
        event = RuntimeEvent(
            event_id="evt-001",
            sequence=1,
            event_type=RuntimeEventType.PROJECT_CREATED,
            occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
            payload={"project_id": "payments"},
        )
        async with broker.subscribe() as queue:
            await broker.publish(event)
            assert await asyncio.wait_for(queue.get(), timeout=0.1) == event

    asyncio.run(scenario())


def test_broker_evicts_oldest_when_queue_full() -> None:
    async def scenario() -> None:
        broker = RuntimeEventBroker(queue_size=2)
        events = [
            RuntimeEvent(
                event_id=f"evt-{i:03d}",
                sequence=i,
                event_type=RuntimeEventType.PROJECT_CREATED,
                occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
                payload={"project_id": f"proj-{i}"},
            )
            for i in range(1, 4)
        ]
        async with broker.subscribe() as queue:
            for event in events:
                await broker.publish(event)
            # Queue size is 2, we published 3 events
            # Oldest (evt-001) should have been evicted
            first = await asyncio.wait_for(queue.get(), timeout=0.1)
            second = await asyncio.wait_for(queue.get(), timeout=0.1)
            assert first.event_id == "evt-002"
            assert second.event_id == "evt-003"
            assert queue.empty()

    asyncio.run(scenario())
