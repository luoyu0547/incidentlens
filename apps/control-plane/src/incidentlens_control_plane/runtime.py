"""Local runtime service container and lifecycle."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    """Container for all runtime services."""

    projects: ProjectRegistryStore
    events: RuntimeEventStore
    broker: RuntimeEventBroker


def build_runtime(settings: RuntimeSettings) -> RuntimeServices:
    """Build and initialize the local runtime services.

    Creates the data directory, initializes SQLite databases,
    and runs migrations for all stores.
    """
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings.data_dir.chmod(0o700)
    database_path = settings.data_dir / "runtime.db"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    projects = ProjectRegistryStore(connect)
    events = RuntimeEventStore(connect)
    projects.migrate()
    events.migrate()
    return RuntimeServices(
        projects=projects,
        events=events,
        broker=RuntimeEventBroker(),
    )
