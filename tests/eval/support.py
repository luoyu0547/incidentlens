"""Test-side assembly for deterministic, real-runtime evaluator scenarios."""

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
from incidentlens_control_plane.investigation.events import InvestigationEventPublisher
from incidentlens_control_plane.investigation.hooks import (
    HookEventType,
    HookRunner,
    RuntimeHookRecorder,
)
from incidentlens_control_plane.investigation.orchestrator import AgentOrchestrator
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.tool_executor import ToolExecutor
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    Investigation,
    InvestigationBudget,
    UsageCounters,
)
from incidentlens_control_plane.logs.service import LogService
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
from incidentlens_control_plane.remote_ops.gateway import RemoteToolGateway
from incidentlens_control_plane.remote_ops.sessions import SessionManager

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
PROJECT_ID = "payments"
TARGET_ID = "dev-a"
SERVICE = "payment-api"
CONTAINER = "payments-api-1"
HOST_ROOT = PurePosixPath("/opt/payments")
CONTAINER_ROOT = PurePosixPath("/app")


@dataclass
class Harness:
    projects: ProjectRegistryStore
    sessions: SessionManager
    investigations: InvestigationStore
    evidence: EvidenceService
    evidence_store: EvidenceStore
    approvals: ApprovalService
    executor: ToolExecutor
    events: RuntimeEventStore
    hooks: HookRunner
    broker: RuntimeEventBroker
    db_path: Path
    transport_factory: Any


def _registration() -> ProjectRegistration:
    return ProjectRegistration(
        project_id=PROJECT_ID,
        display_name="Payments",
        targets=(
            TargetRegistration(
                target_id=TARGET_ID,
                host="dev-a.example.test",
                ssh_user="deploy",
                ssh_config_alias="dev-a",
            ),
        ),
        services=(
            ServiceRegistration(
                compose_service=SERVICE,
                container_names=(CONTAINER,),
                allowed_log_paths=("/var/log/payment/app.log",),
                allowed_host_paths=(HOST_ROOT,),
                allowed_container_paths=(CONTAINER_ROOT,),
                container_path_hints=("/app/logs",),
                protected_remote_paths=(HOST_ROOT / "app.env",),
            ),
        ),
    )


def build_harness(
    tmp_path: Path,
    *,
    transport_factory: Any = None,
) -> Harness:
    db_path = tmp_path / "runtime.db"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    projects = ProjectRegistryStore(connect)
    events = RuntimeEventStore(connect)
    approval_store = ApprovalStore(connect)
    change_store = ChangeSetStore(connect)
    log_store = LogStore(connect)
    evidence_store = EvidenceStore(connect)
    investigations = InvestigationStore(connect)
    for store in (
        projects,
        events,
        approval_store,
        change_store,
        log_store,
        evidence_store,
        investigations,
    ):
        store.migrate()
    if not projects.list():
        projects.create(_registration(), now=NOW)

    broker = RuntimeEventBroker()
    approvals = ApprovalService(approvals=approval_store, events=events, broker=broker)
    factory = transport_factory or FakeTransportFactory()
    sessions = SessionManager(factory)
    changes = ChangeManager(
        store=change_store,
        vault=EncryptedBackupVault(tmp_path / "vault", tmp_path / "vault.key"),
        approvals=approvals,
        events=events,
        broker=broker,
        projects=projects,
        sessions=sessions,
    )
    gateway = RemoteToolGateway(
        projects=projects,
        sessions=sessions,
        changes=changes,
        approvals=approvals,
        events=events,
        broker=broker,
    )
    logs = LogService(
        projects=projects, store=log_store, sessions=sessions, evidence=evidence_store
    )
    evidence = EvidenceService(evidence_store, investigations=investigations)
    hooks = HookRunner()
    recorder = RuntimeHookRecorder(InvestigationEventPublisher(events=events, broker=broker))
    for event_type in HookEventType:
        hooks.register(event_type, recorder)
    executor = ToolExecutor(
        projects=projects,
        sessions=sessions,
        gateway=gateway,
        logs=logs,
        log_store=log_store,
        evidence=evidence,
        evidence_store=evidence_store,
        investigations=investigations,
        approvals=approvals,
        hooks=hooks,
    )
    return Harness(
        projects,
        sessions,
        investigations,
        evidence,
        evidence_store,
        approvals,
        executor,
        events,
        hooks,
        broker,
        db_path,
        factory,
    )


def make_scope(*, container: bool = False) -> AgentScope:
    if container:
        return AgentScope(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            scope=LogScope.CONTAINER,
            service_name=SERVICE,
            container_name=CONTAINER,
            allowed_container_paths=(CONTAINER_ROOT,),
        )
    return AgentScope(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        scope=LogScope.HOST,
        allowed_host_paths=(HOST_ROOT,),
        allowed_container_paths=(CONTAINER_ROOT,),
    )


def seed_run(
    harness: Harness,
    *,
    run_id: str = "run-1",
    investigation_id: str = "inv-1",
    scope: AgentScope | None = None,
    budget: AgentBudget,
    status: Any = None,
) -> AgentRun:
    from incidentlens_control_plane.investigation.state_machine import (
        AgentRunStatus,
        InvestigationStatus,
    )

    investigation = Investigation(
        investigation_id=investigation_id,
        incident_id="inc-1",
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        service=SERVICE,
        symptom="checkout failures",
        status=InvestigationStatus.RUNNING,
        budget=InvestigationBudget(),
        usage=UsageCounters(),
        created_at=NOW,
        updated_at=NOW,
    )
    harness.investigations.create_investigation(investigation)
    run = AgentRun(
        agent_run_id=run_id,
        investigation_id=investigation_id,
        parent_run_id=None,
        kind=AgentRunKind.PARENT,
        scope=scope or make_scope(),
        status=status or AgentRunStatus.CREATED,
        budget=budget,
        usage=UsageCounters(),
        evidence=(),
        created_at=NOW,
        updated_at=NOW,
    )
    harness.investigations.create_agent_run(run)
    return run


def seed_evidence(harness: Harness, *, run_id: str = "run-1", source_ref: str = "seed") -> str:
    record = harness.evidence.record_validation_result(
        agent_run_id=run_id,
        incident_id="inc-1",
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        service_name=SERVICE,
        source_ref=source_ref,
        validator="harness",
        passed=True,
        detail="deterministic seed evidence",
        created_by="harness",
        now=NOW,
    )
    run = harness.investigations.get_agent_run(run_id)
    from incidentlens_control_plane.investigation.types import EvidenceReference

    harness.investigations.update_agent_run(
        run.model_copy(
            update={
                "evidence": (
                    EvidenceReference(
                        evidence_id=record.evidence_ref_id,
                        operation_id=source_ref,
                        summary="seed evidence",
                    ),
                ),
            }
        )
    )
    return record.evidence_ref_id


def make_orchestrator(
    harness: Harness,
    provider: Any,
    *,
    context_manager: Any = None,
    default_budget: AgentBudget | None = None,
) -> AgentOrchestrator:
    return AgentOrchestrator(
        store=harness.investigations,
        provider=provider,
        executor=harness.executor,
        evidence=harness.evidence,
        projects=harness.projects,
        sessions=harness.sessions,
        now=lambda: NOW,
        events=harness.events,
        broker=harness.broker,
        context_manager=context_manager,
        default_budget=default_budget,
        hooks=harness.hooks,
    )
