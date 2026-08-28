from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import PurePosixPath
from typing import Callable

from incidentlens_control_plane.project_registry.types import (
    ProjectRecord,
    ProjectRegistration,
)

_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ProjectAlreadyExists(Exception):
    """Raised when attempting to create a project that already exists."""


class ProjectNotFound(Exception):
    """Raised when a requested project does not exist."""


class ProjectServiceNotFound(Exception):
    """Raised when a registry update names a service that is not registered."""


class RegistryUpdateConflict(Exception):
    """Raised when a proposed registry update is already applied (stale)."""


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

    def list_registry_targets(self) -> tuple[tuple[str, str, str], ...]:
        """Return ``(project_id, registry_target_id, display_name)`` for every
        registered target across all projects.

        This additive accessor exists for the target product facade so it can
        enumerate existing targets (and detect globally unique vs. duplicated
        internal target IDs) without reaching into the ``projects`` table
        directly.  The authoritative record remains ``projects.record_json``.
        """
        return tuple(
            (record.project_id, target.target_id, record.display_name)
            for record in self.list()
            for target in record.targets
        )

    def replace(
        self,
        registration: ProjectRegistration,
        *,
        now: datetime,
        expected_updated_at: datetime | None = None,
    ) -> ProjectRecord:
        """Replace an existing project record. Raises ProjectNotFound if not found.

        When ``expected_updated_at`` is given, the UPDATE is conditional on the
        stored ``updated_at`` matching it, so a concurrent writer that moved the
        record first surfaces as ``RegistryUpdateConflict`` instead of a silent
        lost update.
        """
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

            if expected_updated_at is None:
                cursor = conn.execute(
                    """
                    UPDATE projects
                    SET record_json = ?, created_at = ?, updated_at = ?
                    WHERE project_id = ?
                    """,
                    (record_json, created_at_str, updated_at_str, registration.project_id),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE projects
                    SET record_json = ?, created_at = ?, updated_at = ?
                    WHERE project_id = ? AND updated_at = ?
                    """,
                    (
                        record_json,
                        created_at_str,
                        updated_at_str,
                        registration.project_id,
                        expected_updated_at.isoformat(),
                    ),
                )
            conn.commit()

            if cursor.rowcount == 0:
                raise RegistryUpdateConflict(
                    f"project '{registration.project_id}' was modified concurrently"
                )

        return record

    def derive_registration_with_updates(
        self,
        project: ProjectRecord,
        *,
        service_name: str,
        container: str | None = None,
        host_paths: tuple[PurePosixPath, ...] = (),
    ) -> ProjectRegistration:
        """Derive a new ``ProjectRegistration`` from ``project`` adding a
        container and/or host paths to ``service_name``.

        No database write happens here; the caller persists the result with
        ``replace()`` so an approved writeback stays atomic.  Raises
        ``ProjectServiceNotFound`` when the service is not registered and
        ``RegistryUpdateConflict`` when the container is already registered or
        none of the proposed paths are new (the update is stale / already
        applied by a concurrent writer).
        """
        services = list(project.services)
        index = next(
            (
                i
                for i, svc in enumerate(services)
                if svc.compose_service == service_name
            ),
            None,
        )
        if index is None:
            raise ProjectServiceNotFound(
                f"service {service_name!r} is not registered for "
                f"project {project.project_id!r}"
            )
        svc = services[index]

        new_containers = list(svc.container_names)
        new_host_paths = list(svc.allowed_host_paths)
        added = False

        if container is not None:
            if not _CONTAINER_NAME_RE.match(container):
                raise ValueError(f"invalid container name: {container!r}")
            if container in new_containers:
                raise RegistryUpdateConflict(
                    f"container {container!r} is already registered for "
                    f"service {service_name!r}"
                )
            new_containers.append(container)
            added = True

        for path in host_paths:
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError(f"proposed path must be absolute: {path}")
            if path not in new_host_paths:
                new_host_paths.append(path)
                added = True

        if not added:
            raise RegistryUpdateConflict(
                f"proposed update for service {service_name!r} is already applied"
            )

        updated_svc = svc.model_copy(
            update={
                "container_names": tuple(new_containers),
                "allowed_host_paths": tuple(new_host_paths),
            }
        )
        services[index] = updated_svc
        return ProjectRegistration(
            project_id=project.project_id,
            display_name=project.display_name,
            local_source_paths=project.local_source_paths,
            targets=project.targets,
            services=tuple(services),
        )

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
