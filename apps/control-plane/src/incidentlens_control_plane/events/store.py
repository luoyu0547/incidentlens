import json
import sqlite3
from collections.abc import Callable

from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType


class RuntimeEventStore:
    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connection_factory = connection_factory

    def migrate(self) -> None:
        """Create the events table if it doesn't exist."""
        with self._connection_factory() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def append(self, event: RuntimeEvent) -> RuntimeEvent:
        """Append an event to the store, assigning an auto-incremented sequence."""
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                INSERT INTO runtime_events (event_id, event_type, occurred_at, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_type.value,
                    event.occurred_at.isoformat(),
                    json.dumps(event.payload),
                ),
            )
            conn.commit()
            assigned_sequence = cursor.lastrowid
            return event.model_copy(update={"sequence": assigned_sequence})

    def list_after(self, sequence: int, limit: int = 100) -> tuple[RuntimeEvent, ...]:
        """List events with sequence greater than the given value."""
        if not (1 <= limit <= 1000):
            raise ValueError("limit must be between 1 and 1000")

        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                SELECT sequence, event_id, event_type, occurred_at, payload
                FROM runtime_events
                WHERE sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (sequence, limit),
            )
            rows = cursor.fetchall()

            return tuple(
                RuntimeEvent(
                    sequence=row[0],
                    event_id=row[1],
                    event_type=RuntimeEventType(row[2]),
                    occurred_at=row[3],
                    payload=json.loads(row[4]),
                )
                for row in rows
            )
