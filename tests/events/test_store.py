import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType


def test_event_store_returns_ordered_events_after_cursor(tmp_path: Path) -> None:
    store = RuntimeEventStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    first = RuntimeEvent(
        event_id="evt-001",
        sequence=0,
        event_type=RuntimeEventType.PROJECT_CREATED,
        occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
        payload={"project_id": "payments"},
    )
    second = first.model_copy(
        update={"event_id": "evt-002", "event_type": RuntimeEventType.PROJECT_UPDATED}
    )

    stored_first = store.append(first)
    stored_second = store.append(second)

    assert stored_first.sequence == 1
    assert stored_second.sequence == 2
    assert store.list_after(1, limit=100) == (stored_second,)


def test_product_page_paginates_beyond_1000_and_filters_dimensions(tmp_path: Path) -> None:
    store = RuntimeEventStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    for index in range(1, 1502):
        store.append(
            RuntimeEvent(
                event_id=f"evt-page-{index}",
                event_type=(
                    RuntimeEventType.PROJECT_CREATED
                    if index % 2
                    else RuntimeEventType.PROJECT_UPDATED
                ),
                occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
                payload={"target_id": "target-a" if index <= 1000 else "target-b"},
            )
        )

    sequences: list[int] = []
    after = 0
    while True:
        page = store.list_page(after_sequence=after, limit=500)
        sequences.extend(item.sequence for item in page.items)
        if not page.has_more:
            break
        after = page.next_after_sequence

    assert sequences == list(range(1, 1502))
    filtered = store.list_page(
        after_sequence=0,
        limit=500,
        target_id="target-b",
        event_types=(RuntimeEventType.PROJECT_UPDATED,),
    )
    assert filtered.items
    assert all(item.sequence > 1000 and item.sequence % 2 == 0 for item in filtered.items)


def test_log_subscription_event_types_persist_round_trip(tmp_path: Path) -> None:
    store = RuntimeEventStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    event = RuntimeEvent(
        event_id="evt-log-bp-001",
        sequence=0,
        event_type=RuntimeEventType.LOG_BACKPRESSURE,
        occurred_at=datetime(2026, 8, 12, tzinfo=UTC),
        payload={"subscription_id": "sub-1", "status": "active"},
    )

    stored = store.append(event)

    assert stored.sequence == 1
    assert store.list_after(0, limit=100)[0].event_type == RuntimeEventType.LOG_BACKPRESSURE
