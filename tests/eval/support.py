"""Minimal real-runtime assembly used by deterministic harness scenarios."""
from __future__ import annotations
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.changes.backup import EncryptedBackupVault
from incidentlens_control_plane.changes.manager import ChangeManager
from incidentlens_control_plane.changes.store import ChangeSetStore
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.evidence.service import EvidenceService
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.investigation.hooks import HookEventType, HookRunner, RuntimeHookRecorder
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.tool_executor import ToolExecutor
from incidentlens_control_plane.logs.service import LogService
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import ProjectRegistration, TargetRegistration, ServiceRegistration
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
from incidentlens_control_plane.remote_ops.gateway import RemoteToolGateway
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.investigation.events import InvestigationEventPublisher

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
@dataclass
class Harness:
    projects: Any; sessions: Any; investigations: InvestigationStore; evidence: EvidenceService; evidence_store: EvidenceStore; approvals: ApprovalService; executor: ToolExecutor; events: RuntimeEventStore; hooks: HookRunner; broker: RuntimeEventBroker

def _registration() -> ProjectRegistration:
    return ProjectRegistration(project_id="payments", display_name="Payments", targets=(TargetRegistration(target_id="dev-a", host="dev-a.example.test", ssh_user="deploy", ssh_config_alias="dev-a"),), services=(ServiceRegistration(compose_service="payment-api", container_names=("payments-api-1",), allowed_log_paths=("/var/log/payment/app.log",), allowed_host_paths=(PurePosixPath("/opt/payments"),), allowed_container_paths=(PurePosixPath("/app"),), container_path_hints=("/app/logs",), protected_remote_paths=(PurePosixPath("/opt/payments/app.env"),)),))
def build_harness(tmp_path: Path, *, transport_factory: Any = None) -> Harness:
    db_path = tmp_path / "runtime.db"
    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path); conn.execute("PRAGMA foreign_keys = ON"); return conn
    projects, events, approvals_store, changes_store = ProjectRegistryStore(connect), RuntimeEventStore(connect), ApprovalStore(connect), ChangeSetStore(connect)
    logs_store, evidence_store, investigations = LogStore(connect), EvidenceStore(connect), InvestigationStore(connect)
    for store in (projects, events, approvals_store, changes_store, logs_store, evidence_store, investigations): store.migrate()
    projects.create(_registration(), now=NOW)
    broker = RuntimeEventBroker(); approvals = ApprovalService(approvals=approvals_store, events=events, broker=broker)
    sessions = SessionManager(transport_factory or FakeTransportFactory())
    changes = ChangeManager(store=changes_store, vault=EncryptedBackupVault(tmp_path / "vault", tmp_path / "vault.key"), approvals=approvals, events=events, broker=broker, projects=projects, sessions=sessions)
    gateway = RemoteToolGateway(projects=projects, sessions=sessions, changes=changes, approvals=approvals, events=events, broker=broker)
    logs = LogService(projects=projects, store=logs_store, sessions=sessions, evidence=evidence_store); evidence = EvidenceService(evidence_store, investigations=investigations)
    hooks = HookRunner(); recorder = RuntimeHookRecorder(InvestigationEventPublisher(events=events, broker=broker))
    for event_type in HookEventType: hooks.register(event_type, recorder)
    executor = ToolExecutor(projects=projects, sessions=sessions, gateway=gateway, logs=logs, log_store=logs_store, evidence=evidence, evidence_store=evidence_store, investigations=investigations, approvals=approvals, hooks=hooks)
    return Harness(projects, sessions, investigations, evidence, evidence_store, approvals, executor, events, hooks, broker)
