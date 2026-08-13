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
from incidentlens_control_plane.evidence.service import EvidenceService
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.investigation.fake_provider import (
    FakeProvider,
    FakeProviderRegistry,
)
from incidentlens_control_plane.investigation.orchestrator import AgentOrchestrator
from incidentlens_control_plane.investigation.recovery import RecoveryService
from incidentlens_control_plane.investigation.registry_proposals import (
    RegistryProposalService,
)
from incidentlens_control_plane.investigation.service import InvestigationService
from incidentlens_control_plane.investigation.source_discovery import (
    SourceDiscoveryService,
)
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.tool_executor import ToolExecutor
from incidentlens_control_plane.logs.service import LogService
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.logs.subscriptions import LogSubscriptionManager
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
    subscriptions: LogSubscriptionManager
    evidence_service: EvidenceService
    investigation_store: InvestigationStore
    investigations: InvestigationService
    registry_proposals: RegistryProposalService
    source_discovery: SourceDiscoveryService
    fake_provider: FakeProviderRegistry
    recovery: RecoveryService
    reports: object  # ReportService — 前向引用避免循环导入


def build_runtime(
    settings: RuntimeSettings,
    *,
    transport_factory: RemoteTransportFactory | None = None,
    fake_provider_registry: FakeProviderRegistry | None = None,
) -> RuntimeServices:
    """Build and initialize the local runtime services.

    Creates the data directory, initializes SQLite databases, and runs migrations
    for all stores.  Services are constructed in dependency order: stores and the
    event broker first, then the approval service, session manager, change
    manager, and the remote-tool gateway, then the Phase 4 evidence/provider/tool
    stack, the orchestrator, the investigation service, and finally the recovery
    service (which owns startup recovery and orderly shutdown).
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
    investigation_store = InvestigationStore(connect)
    projects.migrate()
    events.migrate()
    approval_store.migrate()
    change_store.migrate()
    log_store.migrate()
    evidence.migrate()
    investigation_store.migrate()

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
    subscriptions = LogSubscriptionManager(
        store=log_store,
        service=logs,
        events=events,
        broker=broker,
        settings=settings,
    )

    # Phase 4 investigation stack: a scripted provider drives the bounded
    # orchestrator until a production model provider is wired in Task 16, and
    # every service shares the same evidence, approval and event services so
    # there is exactly one execution channel and one event stream.
    evidence_service = EvidenceService(evidence, investigations=investigation_store)
    executor = ToolExecutor(
        projects=projects,
        sessions=sessions,
        gateway=remote_tools,
        logs=logs,
        log_store=log_store,
        evidence=evidence_service,
        evidence_store=evidence,
        investigations=investigation_store,
        approvals=approvals,
    )
    fake_provider = fake_provider_registry or FakeProviderRegistry()
    orchestrator = AgentOrchestrator(
        store=investigation_store,
        provider=FakeProvider(fake_provider),
        executor=executor,
        evidence=evidence_service,
        projects=projects,
        sessions=sessions,
        global_child_limit=settings.max_active_children,
        default_budget=settings.default_run_budget(),
        events=events,
        broker=broker,
    )
    source_discovery = SourceDiscoveryService(
        projects=projects,
        gateway=remote_tools,
        sessions=sessions,
        evidence=evidence_service,
        investigations=investigation_store,
    )
    registry_proposals = RegistryProposalService(
        projects=projects,
        investigations=investigation_store,
        approvals=approvals,
        evidence=evidence_service,
        events=events,
        broker=broker,
        gateway=remote_tools,
        sessions=sessions,
    )
    investigation_service = InvestigationService(
        store=investigation_store,
        orchestrator=orchestrator,
        approvals=approvals,
        executor=executor,
        registry_proposals=registry_proposals,
        events=events,
        broker=broker,
        default_investigation_budget=settings.default_investigation_budget(),
        max_active_investigations=settings.max_active_investigations,
    )
    recovery = RecoveryService(
        store=investigation_store,
        investigations=investigation_service,
        orchestrator=orchestrator,
        evidence=evidence_service,
        approvals=approvals,
        shutdown_grace_seconds=settings.shutdown_grace_seconds,
        events=events,
        broker=broker,
    )

    from incidentlens_control_plane.reports.service import ReportService

    report_dir = settings.report_output_dir or (settings.data_dir / "reports")
    reports = ReportService(
        investigations=investigation_store,
        evidence=evidence,
        output_dir=report_dir,
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
        subscriptions=subscriptions,
        evidence_service=evidence_service,
        investigation_store=investigation_store,
        investigations=investigation_service,
        registry_proposals=registry_proposals,
        source_discovery=source_discovery,
        fake_provider=fake_provider,
        recovery=recovery,
        reports=reports,
    )
