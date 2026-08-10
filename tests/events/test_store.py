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
