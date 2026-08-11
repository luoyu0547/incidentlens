"""Local runtime service container and lifecycle."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.changes.backup import EncryptedBackupVault
from incidentlens_control_plane.changes.manager import ChangeManager
from incidentlens_control_plane.changes.store import ChangeSetStore
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.remote_ops.asyncssh_adapter import (
    AsyncSshTransportFactory,
)
from incidentlens_control_plane.remote_ops.gateway import RemoteToolGateway
from incidentlens_control_plane.remote_ops.sessions import SessionManager


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    """Container for all runtime services."""

    projects: ProjectRegistryStore
    events: RuntimeEventStore
    broker: RuntimeEventBroker
    sessions: SessionManager
    approvals: ApprovalService
    change_store: ChangeSetStore
    backups: EncryptedBackupVault
    changes: ChangeManager
    remote_tools: RemoteToolGateway


def build_runtime(settings: RuntimeSettings) -> RuntimeServices:
    """Build and initialize the local runtime services.

    Creates the data directory, initializes SQLite databases, and runs migrations
    for all stores.  Services are constructed in dependency order: stores and the
    event broker first, then the approval service, session manager, change
    manager, and the remote-tool gateway.
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
    approval_store = ApprovalStore(connect)
    change_store = ChangeSetStore(connect)
    projects.migrate()
    events.migrate()
    approval_store.migrate()
    change_store.migrate()

    broker = RuntimeEventBroker()
    approvals = ApprovalService(
        approvals=approval_store,
        events=events,
        broker=broker,
    )

    sessions = SessionManager(AsyncSshTransportFactory())
    backups = EncryptedBackupVault(
        settings.data_dir / "vault",
        settings.data_dir / "vault.key",
    )

    # No targets are pre-registered at build time; they are resolved per request
    # from the project registry once a project is created.
    changes = ChangeManager(
        store=change_store,
        vault=backups,
        approvals=approvals,
        events=events,
        broker=broker,
        projects=projects,
        sessions=sessions,
        targets={},
    )
    remote_tools = RemoteToolGateway(
        projects=projects,
        sessions=sessions,
        targets={},
        changes=changes,
        approvals=approvals,
        events=events,
        broker=broker,
    )

    return RuntimeServices(
        projects=projects,
        events=events,
        broker=broker,
        sessions=sessions,
        approvals=approvals,
        change_store=change_store,
        backups=backups,
        changes=changes,
        remote_tools=remote_tools,
    )
