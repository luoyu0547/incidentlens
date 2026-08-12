"""Shared fixtures for log pipeline tests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.logs.service import LogService
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.logs.subscriptions import LogSubscriptionManager
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
from incidentlens_control_plane.remote_ops.sessions import SessionManager

_PAYMENTS_NOW = datetime(2026, 8, 12, tzinfo=UTC)


@pytest.fixture
def target_registration() -> TargetRegistration:
    return TargetRegistration(
        target_id="dev-a",
        host="dev-a.example.test",
        ssh_user="deploy",
        ssh_config_alias="dev-a",
    )


@pytest.fixture
def store(tmp_path) -> LogStore:
    """A migrated LogStore over a temp-file SQLite database."""
    log_store = LogStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    log_store.migrate()
    return log_store


@pytest.fixture
async def manager(tmp_path, store, target_registration) -> LogSubscriptionManager:
    """A LogSubscriptionManager wired to the store, a LogService, events, and settings.

    The payments project is registered so reader tasks can resolve the target;
    the fake transport backs the session manager, so no network is touched.
    The manager is closed after each test so reader/writer tasks are cancelled.
    """
    projects = ProjectRegistryStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    projects.migrate()
    projects.create(
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            targets=(target_registration,),
            services=(
                ServiceRegistration(
                    compose_service="payment-api",
                    container_names=("payments-api-1",),
                    allowed_log_paths=("/var/log/payment",),
                ),
            ),
        ),
        now=_PAYMENTS_NOW,
    )
    events = RuntimeEventStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    events.migrate()
    service = LogService(
        projects=projects,
        store=store,
        sessions=SessionManager(FakeTransportFactory()),
    )
    broker = RuntimeEventBroker()
    settings = RuntimeSettings(
        data_dir=tmp_path / "data",
        max_active_log_subscriptions=20,
    )
    mgr = LogSubscriptionManager(
        store=store,
        service=service,
        events=events,
        broker=broker,
        settings=settings,
    )
    yield mgr
    await mgr.close_all()
