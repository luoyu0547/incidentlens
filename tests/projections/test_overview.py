from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.evidence.types import EvidenceKind, EvidenceRef
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    Conclusion,
    Investigation,
    InvestigationBudget,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import (
    LogRecord,
    LogScope,
    LogSeverity,
    LogSourceKind,
)
from incidentlens_control_plane.operations.types import OperationKind, OperationStatus
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.projections.overview import (
    OverviewProjectionService,
    ProjectionWindows,
)
from incidentlens_control_plane.projections.types import HealthStatus
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
from incidentlens_control_plane.runtime import build_runtime

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _runtime(tmp_path: Path):
    return build_runtime(
        RuntimeSettings(data_dir=tmp_path / "runtime"),
        transport_factory=FakeTransportFactory(),
    )


def _register_target(runtime, *, project_id: str, target_id: str, service_name: str) -> str:
    runtime.projects.create(
        ProjectRegistration(
            project_id=project_id,
            display_name=project_id.title(),
            local_source_paths=(Path(f"/srv/{project_id}"),),
            targets=(
                TargetRegistration(
                    target_id=target_id,
                    host=f"{target_id}.example.test",
                    ssh_user="deploy",
                    port=22,
                ),
            ),
            services=(
                ServiceRegistration(
                    compose_service=service_name,
                    container_names=(f"{service_name}-1",),
                    allowed_host_paths=(PurePosixPath(f"/srv/{project_id}"),),
                    protected_remote_paths=(PurePosixPath(f"/srv/{project_id}/.env"),),
                ),
            ),
        ),
        now=NOW,
    )
    return runtime.target_service.get_target(target_id, now=NOW).target_id


def _seed_target_test(runtime, *, target_id: str, reachable: bool, observed_at: datetime) -> None:
    operation = runtime.operation_service.create_operation(
        kind=OperationKind.TARGET_TEST,
        target_id=target_id,
        created_by="tester",
        now=observed_at,
    )
    runtime.operation_service.claim(operation.operation_id, worker="worker-1", now=observed_at)
    runtime.operation_service.transition(
        operation.operation_id,
        OperationStatus.SUCCEEDED,
        progress_summary=f"reachable={reachable}; services=[demo]",
        now=observed_at,
    )


def _seed_log(
    runtime,
    *,
    project_id: str,
    target_id: str,
    service_name: str,
    observed_at: datetime,
    severity: LogSeverity,
    message: str,
) -> None:
    runtime.log_store.append_batch(
        (
            LogRecord(
                log_id=f"log-{hashlib.sha256(message.encode()).hexdigest()[:12]}",
                subscription_id=None,
                project_id=project_id,
                target_id=target_id,
                service_name=service_name,
                source_kind=LogSourceKind.FILE,
                scope=LogScope.HOST,
                source_ref="/var/log/app.log",
                cursor=f"offset:{observed_at.timestamp()}",
                dedupe_key=hashlib.sha256(
                    f"{target_id}:{service_name}:{message}".encode()
                ).hexdigest(),
                observed_at=observed_at,
                event_time=None,
                severity=severity,
                message_redacted=message,
                redaction_summary={},
                normal_signal=None,
                correlation_key=None,
                evidence_ref_id=None,
                created_at=observed_at,
            ),
        )
    )


def _seed_subscription(
    runtime,
    *,
    project_id: str,
    target_id: str,
    service_name: str,
    now: datetime,
    errored: bool = False,
) -> None:
    subscription = runtime.log_store.create_subscription(
        project_id=project_id,
        target_id=target_id,
        service_name=service_name,
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/app.log",
        opt_in_streaming=True,
        created_by="tester",
        now=now,
    )
    if errored:
        runtime.log_store.mark_subscription_error(
            subscription.subscription_id,
            "reader failed",
            now,
        )


def _seed_investigation(
    runtime,
    *,
    investigation_id: str,
    project_id: str,
    target_id: str,
    service_name: str,
    status: InvestigationStatus,
    created_at: datetime,
    completed_at: datetime | None = None,
) -> None:
    runtime.investigation_store.create_investigation(
        Investigation(
            investigation_id=investigation_id,
            incident_id=f"inc-{investigation_id}",
            project_id=project_id,
            target_id=target_id,
            service=service_name,
            symptom=f"{service_name} issue",
            status=status,
            budget=InvestigationBudget(),
            usage=UsageCounters(),
            created_at=created_at,
            updated_at=completed_at or created_at,
            completed_at=completed_at,
        )
    )


def _seed_completed_resolution(
    runtime,
    *,
    investigation_id: str,
    project_id: str,
    target_id: str,
    service_name: str,
    completed_at: datetime,
) -> None:
    _seed_investigation(
        runtime,
        investigation_id=investigation_id,
        project_id=project_id,
        target_id=target_id,
        service_name=service_name,
        status=InvestigationStatus.COMPLETED,
        created_at=completed_at - timedelta(minutes=20),
        completed_at=completed_at,
    )
    runtime.investigation_store.create_agent_run(
        AgentRun(
            agent_run_id=f"run-{investigation_id}",
            investigation_id=investigation_id,
            parent_run_id=None,
            kind=AgentRunKind.PARENT,
            scope=AgentScope(
                project_id=project_id,
                target_id=target_id,
                scope=LogScope.HOST,
                allowed_host_paths=(PurePosixPath("/srv/app"),),
            ),
            status=AgentRunStatus.COMPLETED,
            budget=AgentBudget(),
            usage=UsageCounters(),
            created_at=completed_at - timedelta(minutes=19),
            updated_at=completed_at,
        )
    )
    runtime.investigation_store.create_conclusion(
        agent_run_id=f"run-{investigation_id}",
        investigation_id=investigation_id,
        conclusion=Conclusion(summary="rolled forward a safe fix"),
        now=completed_at,
    )
    runtime.evidence.create(
        EvidenceRef(
            evidence_ref_id=f"ev-{investigation_id}",
            incident_id=f"inc-{investigation_id}",
            evidence_kind=EvidenceKind.VALIDATION_RESULT,
            agent_run_id=f"run-{investigation_id}",
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            source_ref="validator:health",
            source_kind=None,
            scope=None,
            cursor=None,
            severity=None,
            event_time=None,
            normal_signal=None,
            correlation_key=None,
            content_redacted="health endpoint returned 200",
            content_sha256=hashlib.sha256(b"health endpoint returned 200").hexdigest(),
            redaction_summary={},
            truncation=None,
            metadata={"validator": "health", "passed": "true"},
            created_at=completed_at,
            created_by="tester",
        )
    )


def test_overview_projection_derives_counts_statuses_and_recent_resolutions(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    healthy_target = _register_target(
        runtime, project_id="payments", target_id="dev-a", service_name="payment-api"
    )
    degraded_target = _register_target(
        runtime, project_id="orders", target_id="dev-b", service_name="orders-api"
    )
    unreachable_target = _register_target(
        runtime, project_id="billing", target_id="dev-c", service_name="billing-api"
    )
    unknown_target = _register_target(
        runtime, project_id="auth", target_id="dev-d", service_name="auth-api"
    )

    _seed_target_test(runtime, target_id=healthy_target, reachable=True, observed_at=NOW)
    _seed_subscription(
        runtime,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        now=NOW - timedelta(minutes=5),
    )

    _seed_subscription(
        runtime,
        project_id="orders",
        target_id="dev-b",
        service_name="orders-api",
        now=NOW - timedelta(minutes=3),
        errored=True,
    )
    _seed_log(
        runtime,
        project_id="orders",
        target_id="dev-b",
        service_name="orders-api",
        observed_at=NOW - timedelta(minutes=2),
        severity=LogSeverity.ERROR,
        message="ERROR checkout failed",
    )

    _seed_target_test(
        runtime,
        target_id=unreachable_target,
        reachable=False,
        observed_at=NOW - timedelta(minutes=4),
    )

    asyncio.run(
        runtime.approvals.request(
            {"kind": "docker.restart", "target_id": healthy_target, "service": "payment-api"},
            now=NOW - timedelta(minutes=1),
            target_id=healthy_target,
            service="payment-api",
        )
    )
    _seed_investigation(
        runtime,
        investigation_id="inv-open-1",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        status=InvestigationStatus.RUNNING,
        created_at=NOW - timedelta(minutes=10),
    )
    _seed_investigation(
        runtime,
        investigation_id="inv-failed-1",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        status=InvestigationStatus.FAILED,
        created_at=NOW - timedelta(minutes=12),
    )
    _seed_completed_resolution(
        runtime,
        investigation_id="inv-resolved-1",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        completed_at=NOW - timedelta(minutes=6),
    )

    projection = OverviewProjectionService(
        target_service=runtime.target_service,
        target_store=runtime.target_store,
        projects=runtime.projects,
        approvals=runtime.approvals._approvals,
        investigations=runtime.investigation_store,
        operations=runtime.operation_store,
        logs=runtime.log_store,
        evidence=runtime.evidence,
        now=lambda: NOW,
        windows=ProjectionWindows(),
    )

    view = projection.read_overview()

    assert view.service_counts.healthy == 1
    assert view.service_counts.degraded == 1
    assert view.service_counts.unreachable == 1
    assert view.service_counts.unknown == 1
    assert view.open_issue_count == 2
    assert view.active_investigation_count == 1
    assert view.pending_approval_count == 1
    assert [target.status for target in view.targets] == [
        HealthStatus.HEALTHY,
        HealthStatus.DEGRADED,
        HealthStatus.UNREACHABLE,
        HealthStatus.UNKNOWN,
    ]
    assert view.recent_resolutions[0].investigation_id == "inv-resolved-1"
    assert view.recent_resolutions[0].resolution_summary == "rolled forward a safe fix"
    assert view.recent_resolutions[0].verification_summary == "health endpoint returned 200"


def test_overview_projection_returns_unknown_without_recent_evidence(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    target_id = _register_target(
        runtime, project_id="legacy", target_id="dev-z", service_name="legacy-api"
    )
    old_time = NOW - timedelta(hours=3)
    _seed_target_test(runtime, target_id=target_id, reachable=True, observed_at=old_time)
    _seed_subscription(
        runtime,
        project_id="legacy",
        target_id="dev-z",
        service_name="legacy-api",
        now=old_time,
    )

    projection = OverviewProjectionService(
        target_service=runtime.target_service,
        target_store=runtime.target_store,
        projects=runtime.projects,
        approvals=runtime.approvals._approvals,
        investigations=runtime.investigation_store,
        operations=runtime.operation_store,
        logs=runtime.log_store,
        evidence=runtime.evidence,
        now=lambda: NOW,
        windows=ProjectionWindows(
            target_test_lookback=timedelta(minutes=15),
            subscription_lookback=timedelta(minutes=15),
            error_lookback=timedelta(minutes=15),
            resolution_lookback=timedelta(days=7),
            max_recent_resolutions=5,
        ),
    )

    view = projection.read_overview()

    assert view.targets[0].status is HealthStatus.UNKNOWN
    assert view.service_counts.unknown == 1
