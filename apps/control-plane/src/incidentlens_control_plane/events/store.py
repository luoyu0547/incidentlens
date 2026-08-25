import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType


@dataclass(frozen=True)
class RuntimeEventPage:
    items: tuple[RuntimeEvent, ...]
    next_after_sequence: int
    has_more: bool
    latest_sequence: int
    earliest_available_sequence: int


class RuntimeEventStore:
    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connection_factory = connection_factory

    def migrate(self) -> None:
        """Create or upgrade the events table and its product indexes."""
        with self._connection_factory() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    session_id TEXT,
                    target_id TEXT,
                    investigation_id TEXT
                );
                """
            )
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(runtime_events)").fetchall()
            }
            for name, declaration in (
                ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
                ("session_id", "TEXT"),
                ("target_id", "TEXT"),
                ("investigation_id", "TEXT"),
            ):
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE runtime_events ADD COLUMN {name} {declaration}"
                    )
            conn.execute(
                """
                UPDATE runtime_events
                SET session_id = COALESCE(session_id, json_extract(payload, '$.session_id')),
                    target_id = COALESCE(target_id, json_extract(payload, '$.target_id')),
                    investigation_id = COALESCE(
                        investigation_id, json_extract(payload, '$.investigation_id')
                    )
                WHERE session_id IS NULL OR target_id IS NULL OR investigation_id IS NULL
                """
            )
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_runtime_events_session_sequence
                    ON runtime_events(session_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_runtime_events_target_sequence
                    ON runtime_events(target_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_runtime_events_investigation_sequence
                    ON runtime_events(investigation_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_runtime_events_type_sequence
                    ON runtime_events(event_type, sequence);
                """
            )
            conn.commit()

    def append(self, event: RuntimeEvent) -> RuntimeEvent:
        """Append an event to the store, assigning an auto-incremented sequence."""
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                INSERT INTO runtime_events (
                    event_id, event_type, occurred_at, payload,
                    schema_version, session_id, target_id, investigation_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_type.value,
                    event.occurred_at.isoformat(),
                    json.dumps(event.payload),
                    1,
                    _payload_string(event.payload, "session_id"),
                    _payload_string(event.payload, "target_id"),
                    _payload_string(event.payload, "investigation_id"),
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

            return tuple(_event_from_row(row) for row in rows)

    def list_page(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        session_id: str | None = None,
        target_id: str | None = None,
        investigation_id: str | None = None,
        event_types: tuple[RuntimeEventType, ...] = (),
        allowed_target_ids: frozenset[str] | None = None,
    ) -> RuntimeEventPage:
        """Return a bounded, filtered page without exposing payload internals."""
        if after_sequence < 0 or not (1 <= limit <= 500):
            raise ValueError("invalid event page bounds")
        clauses = ["sequence > ?"]
        params: list[object] = [after_sequence]
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if target_id is not None:
            clauses.append("target_id = ?")
            params.append(target_id)
        if investigation_id is not None:
            clauses.append("investigation_id = ?")
            params.append(investigation_id)
        if event_types:
            placeholders = ", ".join("?" for _ in event_types)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(event_type.value for event_type in event_types)
        if allowed_target_ids is not None:
            if not allowed_target_ids:
                clauses.append("1 = 0")
            else:
                placeholders = ", ".join("?" for _ in allowed_target_ids)
                clauses.append(f"(target_id IS NULL OR target_id IN ({placeholders}))")
                params.extend(sorted(allowed_target_ids))
        where = " AND ".join(clauses)
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT sequence, event_id, event_type, occurred_at, payload
                FROM runtime_events
                WHERE {where}
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (*params, limit + 1),
            ).fetchall()
            latest = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM runtime_events"
            ).fetchone()[0]
            earliest = conn.execute(
                "SELECT COALESCE(MIN(sequence), 0) FROM runtime_events"
            ).fetchone()[0]
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = tuple(_event_from_row(row) for row in selected)
        next_sequence = items[-1].sequence if items else after_sequence
        return RuntimeEventPage(
            items=items,
            next_after_sequence=next_sequence,
            has_more=has_more,
            latest_sequence=int(latest),
            earliest_available_sequence=int(earliest),
        )


def _payload_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _event_from_row(row) -> RuntimeEvent:
    return RuntimeEvent(
        sequence=row[0],
        event_id=row[1],
        event_type=RuntimeEventType(row[2]),
        occurred_at=row[3],
        payload=json.loads(row[4]),
    )


__all__ = ["RuntimeEventPage", "RuntimeEventStore"]
