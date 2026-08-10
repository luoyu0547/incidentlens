"""Local runtime configuration and service construction."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore


class RuntimeSettings(BaseModel):
    """Immutable settings for the local runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: Path

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        """Create settings from the INCIDENTLENS_DATA_DIR environment variable."""
        configured = os.environ.get("INCIDENTLENS_DATA_DIR")
        data_dir = Path(configured).expanduser() if configured else Path.home() / ".incidentlens"
        return cls(data_dir=data_dir.resolve())


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    """Bundle of services available during the application lifetime."""

    projects: ProjectRegistryStore
    events: RuntimeEventStore
    broker: RuntimeEventBroker


def build_runtime(settings: RuntimeSettings) -> RuntimeServices:
    """Construct and migrate all runtime services."""
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
