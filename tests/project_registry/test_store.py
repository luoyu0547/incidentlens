import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from incidentlens_control_plane.project_registry.store import (
    ProjectAlreadyExists,
    ProjectNotFound,
    ProjectRegistryStore,
)
from incidentlens_control_plane.project_registry.types import ProjectRegistration


def connection_factory(path: Path):
    return lambda: sqlite3.connect(path)


def registration(path: Path, name: str = "Payments") -> ProjectRegistration:
    return ProjectRegistration(
        project_id="payments",
        display_name=name,
        local_source_paths=(path.resolve(),),
    )


def test_store_round_trips_project_across_connections(tmp_path: Path) -> None:
    store = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))
    store.migrate()
    now = datetime(2026, 8, 10, tzinfo=UTC)

    created = store.create(registration(tmp_path), now=now)
    reopened = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))

    assert reopened.get("payments") == created
    assert reopened.list() == (created,)


def test_store_rejects_duplicate_and_missing_projects(tmp_path: Path) -> None:
    store = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))
    store.migrate()
    now = datetime(2026, 8, 10, tzinfo=UTC)
    store.create(registration(tmp_path), now=now)

    with pytest.raises(ProjectAlreadyExists):
        store.create(registration(tmp_path), now=now)
    with pytest.raises(ProjectNotFound):
        store.get("unknown")


def test_replace_preserves_created_at_and_updates_updated_at(tmp_path: Path) -> None:
    store = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))
    store.migrate()
    created_at = datetime(2026, 8, 10, tzinfo=UTC)
    store.create(registration(tmp_path), now=created_at)

    replaced = store.replace(
        registration(tmp_path, name="Payments API"),
        now=created_at + timedelta(minutes=5),
    )

    assert replaced.created_at == created_at
    assert replaced.updated_at == created_at + timedelta(minutes=5)
