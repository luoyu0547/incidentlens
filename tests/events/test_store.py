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


def test_product_page_unions_session_and_investigation_history(tmp_path: Path) -> None:
    store = RuntimeEventStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    base = RuntimeEvent(
        event_id="evt-session-text",
        event_type=RuntimeEventType.AGENT_TEXT_DELTA,
        occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
        payload={"session_id": "session-1", "message_id": "message-1", "text": "done"},
    )
    store.append(base)
    store.append(
        base.model_copy(
            update={
                "event_id": "evt-investigation-tool",
                "event_type": RuntimeEventType.TOOL_PROPOSED,
                "payload": {
                    "investigation_id": "investigation-1",
                    "tool_call_id": "call-1",
                    "tool_name": "registry_info",
                },
            }
        )
    )
    store.append(
        base.model_copy(
            update={
                "event_id": "evt-other-tool",
                "event_type": RuntimeEventType.TOOL_PROPOSED,
                "payload": {
                    "investigation_id": "investigation-2",
                    "tool_call_id": "call-2",
                    "tool_name": "registry_info",
                },
            }
        )
    )

    page = store.list_page(
        after_sequence=0,
        limit=500,
        session_id="session-1",
        investigation_id="investigation-1",
    )

    assert [event.event_id for event in page.items] == [
        "evt-session-text",
        "evt-investigation-tool",
    ]


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
