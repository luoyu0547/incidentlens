from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable

from incidentlens_control_plane.project_registry.types import (
    ProjectRecord,
    ProjectRegistration,
)


class ProjectAlreadyExists(Exception):
    """Raised when attempting to create a project that already exists."""


class ProjectNotFound(Exception):
    """Raised when a requested project does not exist."""


class ProjectRegistryStore:
    """SQLite-backed store for project registrations."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connection_factory = connection_factory

    def migrate(self) -> None:
        """Create the projects table if it doesn't exist."""
        with self._connection_factory() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def create(
        self, registration: ProjectRegistration, *, now: datetime
    ) -> ProjectRecord:
        """Create a new project record. Raises ProjectAlreadyExists if it exists."""
        record = ProjectRecord.from_registration(registration, created_at=now)
        record_json = record.model_dump_json()
        created_at_str = record.created_at.isoformat()
        updated_at_str = record.updated_at.isoformat()

        with self._connection_factory() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO projects (project_id, record_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (record.project_id, record_json, created_at_str, updated_at_str),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                raise ProjectAlreadyExists(
                    f"Project '{record.project_id}' already exists"
                )

        return record

    def get(self, project_id: str) -> ProjectRecord:
        """Retrieve a project by ID. Raises ProjectNotFound if not found."""
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                SELECT record_json FROM projects WHERE project_id = ?
                """,
                (project_id,),
            )
            row = cursor.fetchone()

        if row is None:
            raise ProjectNotFound(f"Project '{project_id}' not found")

        return ProjectRecord.model_validate_json(row[0])

    def list(self) -> tuple[ProjectRecord, ...]:
        """List all projects, sorted by project_id."""
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                SELECT record_json FROM projects ORDER BY project_id
                """
            )
            rows = cursor.fetchall()

        return tuple(ProjectRecord.model_validate_json(row[0]) for row in rows)

    def replace(
        self, registration: ProjectRegistration, *, now: datetime
    ) -> ProjectRecord:
        """Replace an existing project record. Raises ProjectNotFound if not found."""
        with self._connection_factory() as conn:
            # First, get the existing record to preserve created_at
            cursor = conn.execute(
                """
                SELECT created_at FROM projects WHERE project_id = ?
                """,
                (registration.project_id,),
            )
            row = cursor.fetchone()

            if row is None:
                raise ProjectNotFound(
                    f"Project '{registration.project_id}' not found"
                )

            created_at_str = row[0]
            created_at = datetime.fromisoformat(created_at_str)

            # Create updated record preserving original created_at
            record = ProjectRecord.from_registration(registration, created_at=created_at)
            record = record.model_copy(update={"updated_at": now})
            record_json = record.model_dump_json()
            updated_at_str = record.updated_at.isoformat()

            conn.execute(
                """
                UPDATE projects
                SET record_json = ?, created_at = ?, updated_at = ?
                WHERE project_id = ?
                """,
                (record_json, created_at_str, updated_at_str, registration.project_id),
            )
            conn.commit()

        return record

    def delete(self, project_id: str) -> None:
        """Delete a project by ID. Raises ProjectNotFound if not found."""
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                DELETE FROM projects WHERE project_id = ?
                """,
                (project_id,),
            )
            conn.commit()

            if cursor.rowcount == 0:
                raise ProjectNotFound(f"Project '{project_id}' not found")
