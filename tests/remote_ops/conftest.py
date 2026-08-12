"""Shared fixtures for remote-ops tests."""

from __future__ import annotations

import sqlite3
from pathlib import PurePosixPath

import pytest
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import (
    ServiceRegistration,
    TargetRegistration,
)


@pytest.fixture
def project_store(tmp_path) -> ProjectRegistryStore:
    """A real SQLite-backed project registry with an empty test database."""
    store = ProjectRegistryStore(lambda: sqlite3.connect(tmp_path / "projects.db"))
    store.migrate()
    return store


@pytest.fixture
def target_registration() -> TargetRegistration:
    return TargetRegistration(
        target_id="dev-a",
        host="dev-a.example.test",
        ssh_user="deploy",
        ssh_config_alias="dev-a",
    )


@pytest.fixture
def service_registration() -> ServiceRegistration:
    return ServiceRegistration(
        compose_service="payment-api",
        container_names=("payments-api-1", "payments-api-2"),
        allowed_host_paths=(PurePosixPath("/opt/payments"),),
        allowed_container_paths=(PurePosixPath("/app"),),
    )
