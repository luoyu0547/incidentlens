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
