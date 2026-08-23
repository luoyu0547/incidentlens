"""SQLite persistence for project-scoped, evidence-backed Project Memory.

Follows the runtime.db / sqlite3 conventions of the sibling stores (investigations,
evidence, approvals): an idempotent additive ``migrate()``, validated Pydantic JSON
in every ``record_json`` column, and denormalized project/status index columns so
active selection stays cheap.  Supersession is a status transition on the existing
row (``ACTIVE`` -> ``SUPERSEDED``), so a historical record and its provenance are
preserved rather than destructively overwritten -- matching how Session Memory
revisions and transcripts are kept append-only.

Only validated ``ProjectMemoryEntry`` contracts ever reach a ``record_json``
column.  Raw tool output, unverified hypotheses and secrets are rejected by the
service before they reach this store.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime

from incidentlens_control_plane.project_memory.types import (
    ProjectMemoryEntry,
    ProjectMemoryStatus,
)

_MEMORY_COLUMNS = (
    "memory_id",
    "project_id",
    "status",
    "record_json",
    "created_at",
    "last_confirmed_at",
)


class ProjectMemoryNotFound(Exception):
    """Raised when a project-memory row is missing."""


def _iso(value: datetime) -> str:
    return value.isoformat()


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _derive_validated(model: ProjectMemoryEntry, updates: dict[str, object]) -> ProjectMemoryEntry:
    """Derive a new validated contract from ``model`` plus ``updates``.

    ``model_copy(update=...)`` skips validation, so re-validating every field
    keeps the persisted ``record_json`` a valid contract.
    """
    return type(model).model_validate({**model.model_dump(), **updates})


class ProjectMemoryStore:
    """SQLite-backed persistence for project-scoped Project Memory."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connection_factory = connection_factory

    def migrate(self) -> None:
        """Create the project_memories table and indexes if they don't exist."""
        with self._connection_factory() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS project_memories (
                    memory_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_confirmed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_project_memories_project_status
                    ON project_memories(project_id, status, last_confirmed_at);
                CREATE INDEX IF NOT EXISTS idx_project_memories_status
                    ON project_memories(status);
                """
            )
            conn.commit()

    def upsert(self, entry: ProjectMemoryEntry) -> ProjectMemoryEntry:
        """Write one memory row keyed on ``memory_id`` (idempotent upsert)."""
        with self._connection_factory() as conn:
            conn.execute(
                f"""
                INSERT OR REPLACE INTO project_memories ({", ".join(_MEMORY_COLUMNS)})
                VALUES ({_placeholders(len(_MEMORY_COLUMNS))})
                """,
                (
                    entry.memory_id,
                    entry.project_id,
                    entry.status.value,
                    entry.model_dump_json(),
                    _iso(entry.created_at),
                    _iso(entry.last_confirmed_at),
                ),
            )
            conn.commit()
        return entry

    def get(self, memory_id: str) -> ProjectMemoryEntry:
        """Return the record with the given id (any status), or raise NotFound."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_MEMORY_COLUMNS)}
                FROM project_memories WHERE memory_id = ?
                """,
                (memory_id,),
            ).fetchone()
        if row is None:
            raise ProjectMemoryNotFound(f"project memory not found: {memory_id}")
        return ProjectMemoryEntry.model_validate_json(row[3])

    def list_active(
        self, project_id: str, limit: int = 5
    ) -> tuple[ProjectMemoryEntry, ...]:
        """Return ACTIVE records for a project, most recently confirmed first."""
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_MEMORY_COLUMNS)}
                FROM project_memories
                WHERE project_id = ? AND status = ?
                ORDER BY last_confirmed_at DESC, memory_id ASC
                LIMIT ?
                """,
                (project_id, ProjectMemoryStatus.ACTIVE.value, limit),
            ).fetchall()
        return tuple(ProjectMemoryEntry.model_validate_json(row[3]) for row in rows)

    def supersede(self, memory_id: str) -> ProjectMemoryEntry:
        """Move one ACTIVE record to SUPERSEDED, preserving the historical row.

        The conditional UPDATE keys on the current ACTIVE status, so an already
        superseded record is a no-op and a concurrent writer cannot re-apply.
        Provenance is preserved unchanged.
        """
        with self._connection_factory() as conn:
            row = conn.execute(
                "SELECT record_json FROM project_memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise ProjectMemoryNotFound(f"project memory not found: {memory_id}")
            current = ProjectMemoryEntry.model_validate_json(row[0])
            if current.status is not ProjectMemoryStatus.ACTIVE:
                return current
            replaced = _derive_validated(
                current, {"status": ProjectMemoryStatus.SUPERSEDED}
            )
            cursor = conn.execute(
                """
                UPDATE project_memories
                SET record_json = ?, status = ?
                WHERE memory_id = ? AND status = ?
                """,
                (
                    replaced.model_dump_json(),
                    replaced.status.value,
                    memory_id,
                    ProjectMemoryStatus.ACTIVE.value,
                ),
            )
            conn.commit()
            if cursor.rowcount == 0:
                return current
        return replaced
