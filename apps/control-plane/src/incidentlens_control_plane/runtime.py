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
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.logs.service import LogService
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.remote_ops.asyncssh_adapter import (
    AsyncSshTransportFactory,
)
from incidentlens_control_plane.remote_ops.gateway import RemoteToolGateway
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.transport import RemoteTransportFactory


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
    log_store: LogStore
    evidence: EvidenceStore
    logs: LogService


def build_runtime(
    settings: RuntimeSettings,
    *,
    transport_factory: RemoteTransportFactory | None = None,
) -> RuntimeServices:
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
    log_store = LogStore(connect)
    evidence = EvidenceStore(connect)
    projects.migrate()
    events.migrate()
    approval_store.migrate()
    change_store.migrate()
    log_store.migrate()
    evidence.migrate()

    broker = RuntimeEventBroker()
    approvals = ApprovalService(
        approvals=approval_store,
        events=events,
        broker=broker,
    )

    sessions = SessionManager(transport_factory or AsyncSshTransportFactory())
    backups = EncryptedBackupVault(
        settings.data_dir / "vault",
        settings.data_dir / "vault.key",
    )

    # No targets are pre-registered at build time; both services resolve the
    # target from the project record's ``targets`` per request.
    changes = ChangeManager(
        store=change_store,
        vault=backups,
        approvals=approvals,
        events=events,
        broker=broker,
        projects=projects,
        sessions=sessions,
    )
    remote_tools = RemoteToolGateway(
        projects=projects,
        sessions=sessions,
        changes=changes,
        approvals=approvals,
        events=events,
        broker=broker,
    )
    logs = LogService(
        projects=projects,
        store=log_store,
        sessions=sessions,
        evidence=evidence,
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
        log_store=log_store,
        evidence=evidence,
        logs=logs,
    )
