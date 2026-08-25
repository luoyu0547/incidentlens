from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.investigation.state_machine import InvestigationStatus
from incidentlens_control_plane.investigation.types import (
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
from incidentlens_control_plane.projections.services import (
    ProjectionWindows,
    ServiceProjectionService,
)
from incidentlens_control_plane.projections.types import HealthStatus
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
from incidentlens_control_plane.runtime import build_runtime

NOW = datetime(2026, 8, 25, 13, 0, tzinfo=UTC)


def _runtime(tmp_path: Path):
    return build_runtime(
        RuntimeSettings(data_dir=tmp_path / "runtime"),
        transport_factory=FakeTransportFactory(),
    )


def _seed(runtime) -> tuple[str, str]:
    runtime.projects.create(
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            local_source_paths=(Path("/srv/payments"),),
            targets=(
                TargetRegistration(
                    target_id="dev-a",
                    host="dev-a.example.test",
                    ssh_user="deploy",
                    port=22,
                ),
            ),
            services=(
                ServiceRegistration(
                    compose_service="payment-api",
                    container_names=("payments-api-1", "payments-api-2"),
                    allowed_host_paths=(PurePosixPath("/srv/payments"),),
                    protected_remote_paths=(PurePosixPath("/srv/payments/.env"),),
                ),
            ),
        ),
        now=NOW,
    )
    target = runtime.target_service.get_target("dev-a", now=NOW).target_id
    operation = runtime.operation_service.create_operation(
        kind=OperationKind.TARGET_TEST,
        target_id=target,
        created_by="tester",
        now=NOW - timedelta(minutes=4),
    )
    runtime.operation_service.claim(
        operation.operation_id, worker="worker-1", now=NOW - timedelta(minutes=4)
    )
    runtime.operation_service.transition(
        operation.operation_id,
        OperationStatus.SUCCEEDED,
        progress_summary="reachable=True; services=[payment-api]",
        now=NOW - timedelta(minutes=4),
    )
    runtime.log_store.create_subscription(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payments/api.log",
        opt_in_streaming=True,
        created_by="tester",
        now=NOW - timedelta(minutes=3),
    )
    runtime.log_store.append_batch(
        (
            LogRecord(
                log_id="log-1",
                subscription_id=None,
                project_id="payments",
                target_id="dev-a",
                service_name="payment-api",
                source_kind=LogSourceKind.FILE,
                scope=LogScope.HOST,
                source_ref="/var/log/payments/api.log",
                cursor="offset:1",
                dedupe_key=hashlib.sha256(b"log-1").hexdigest(),
                observed_at=NOW - timedelta(minutes=2),
                event_time=None,
                severity=LogSeverity.INFO,
                message_redacted="INFO payment ok",
                redaction_summary={},
                normal_signal=None,
                correlation_key=None,
                evidence_ref_id=None,
                created_at=NOW - timedelta(minutes=2),
            ),
        )
    )
    asyncio.run(
        runtime.approvals.request(
            {
                "kind": "docker.restart",
                "target_id": target,
                "service": "payment-api",
                "argv": ["docker", "restart", "payments-api-1"],
            },
            now=NOW - timedelta(minutes=1),
            target_id=target,
            service="payment-api",
        )
    )
    runtime.investigation_store.create_investigation(
        Investigation(
            investigation_id="inv-1",
            incident_id="inc-1",
            project_id="payments",
            target_id="dev-a",
            service="payment-api",
            symptom="checkout errors",
            status=InvestigationStatus.RUNNING,
            budget=InvestigationBudget(),
            usage=UsageCounters(),
            created_at=NOW - timedelta(minutes=5),
            updated_at=NOW - timedelta(minutes=1),
        )
    )
    return target, "payment-api"


def test_service_projection_returns_safe_summary_without_sensitive_fields(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    target_id, service_id = _seed(runtime)
    projection = ServiceProjectionService(
        target_service=runtime.target_service,
        target_store=runtime.target_store,
        projects=runtime.projects,
        approvals=runtime.approvals._approvals,
        investigations=runtime.investigation_store,
        operations=runtime.operation_store,
        logs=runtime.log_store,
        now=lambda: NOW,
        windows=ProjectionWindows(),
    )

    view = projection.read_service(service_id)

    assert view.service_id == service_id
    assert view.status is HealthStatus.HEALTHY
    assert view.target_ids == (target_id,)
    assert view.issue_ids == ("iss_inv-1",)
    assert view.investigation_ids == ("inv-1",)
    assert view.pending_approval_count == 1
    assert view.instances[0].pending_approval_count == 1
    assert view.instances[0].container_names == ("payments-api-1", "payments-api-2")
    assert view.log_sources[0].source_kind is LogSourceKind.FILE
    dumped = view.model_dump_json()
    assert "/srv/payments/.env" not in dumped
    assert "/var/log/payments/api.log" not in dumped
    assert "docker restart" not in dumped
    assert "authentication_ref" not in dumped
    assert "tool_call_id" not in dumped
    assert "pending_approval_ids" not in dumped


def test_service_projection_returns_none_when_not_authorized_for_any_target(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _seed(runtime)
    projection = ServiceProjectionService(
        target_service=runtime.target_service,
        target_store=runtime.target_store,
        projects=runtime.projects,
        approvals=runtime.approvals._approvals,
        investigations=runtime.investigation_store,
        operations=runtime.operation_store,
        logs=runtime.log_store,
        now=lambda: NOW,
        windows=ProjectionWindows(),
    )

    assert (
        projection.read_service("payment-api", allowed_target_ids=frozenset({"other-target"}))
        is None
    )


def test_service_projection_failed_investigation_degrades_health(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _seed(runtime)
    runtime.investigation_store.transition_investigation_status(
        "inv-1",
        InvestigationStatus.FAILED,
        now=NOW,
    )
    projection = ServiceProjectionService(
        target_service=runtime.target_service,
        target_store=runtime.target_store,
        projects=runtime.projects,
        approvals=runtime.approvals._approvals,
        investigations=runtime.investigation_store,
        operations=runtime.operation_store,
        logs=runtime.log_store,
        now=lambda: NOW,
        windows=ProjectionWindows(),
    )

    view = projection.read_service("payment-api")

    assert view is not None
    assert view.status is HealthStatus.DEGRADED


def test_service_projection_healthy_plus_unknown_aggregates_to_unknown(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.projects.create(
        ProjectRegistration(
            project_id="fleet",
            display_name="Fleet",
            local_source_paths=(Path("/srv/fleet"),),
            targets=(
                TargetRegistration(
                    target_id="dev-a",
                    host="dev-a.example.test",
                    ssh_user="deploy",
                    port=22,
                ),
                TargetRegistration(
                    target_id="dev-b",
                    host="dev-b.example.test",
                    ssh_user="deploy",
                    port=22,
                ),
            ),
            services=(
                ServiceRegistration(
                    compose_service="payment-api",
                    container_names=("payments-api-1",),
                    allowed_host_paths=(PurePosixPath("/srv/fleet"),),
                    protected_remote_paths=(PurePosixPath("/srv/fleet/.env"),),
                ),
            ),
        ),
        now=NOW,
    )
    healthy_target = runtime.target_service.get_target("dev-a", now=NOW).target_id
    _ = runtime.target_service.get_target("dev-b", now=NOW).target_id
    operation = runtime.operation_service.create_operation(
        kind=OperationKind.TARGET_TEST,
        target_id=healthy_target,
        created_by="tester",
        now=NOW - timedelta(minutes=4),
    )
    runtime.operation_service.claim(
        operation.operation_id, worker="worker-1", now=NOW - timedelta(minutes=4)
    )
    runtime.operation_service.transition(
        operation.operation_id,
        OperationStatus.SUCCEEDED,
        progress_summary="reachable=True; services=[payment-api]",
        now=NOW - timedelta(minutes=4),
    )

    projection = ServiceProjectionService(
        target_service=runtime.target_service,
        target_store=runtime.target_store,
        projects=runtime.projects,
        approvals=runtime.approvals._approvals,
        investigations=runtime.investigation_store,
        operations=runtime.operation_store,
        logs=runtime.log_store,
        now=lambda: NOW,
        windows=ProjectionWindows(),
    )

    view = projection.read_service("payment-api")

    assert view is not None
    assert tuple(instance.status for instance in view.instances) == (
        HealthStatus.HEALTHY,
        HealthStatus.UNKNOWN,
    )
    assert view.status is HealthStatus.UNKNOWN


def test_service_projection_ignores_colliding_registry_target_ids_for_approvals(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.projects.create(
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            local_source_paths=(Path("/srv/payments"),),
            targets=(
                TargetRegistration(
                    target_id="default",
                    host="dev-a.example.test",
                    ssh_user="deploy",
                    port=22,
                ),
            ),
            services=(
                ServiceRegistration(
                    compose_service="shared-api",
                    container_names=("shared-api-1",),
                    allowed_host_paths=(PurePosixPath("/srv/payments"),),
                    protected_remote_paths=(PurePosixPath("/srv/payments/.env"),),
                ),
            ),
        ),
        now=NOW,
    )
    runtime.projects.create(
        ProjectRegistration(
            project_id="ops",
            display_name="Ops",
            local_source_paths=(Path("/srv/ops"),),
            targets=(
                TargetRegistration(
                    target_id="default",
                    host="dev-b.example.test",
                    ssh_user="deploy",
                    port=22,
                ),
            ),
            services=(
                ServiceRegistration(
                    compose_service="shared-api",
                    container_names=("shared-api-2",),
                    allowed_host_paths=(PurePosixPath("/srv/ops"),),
                    protected_remote_paths=(PurePosixPath("/srv/ops/.env"),),
                ),
            ),
        ),
        now=NOW,
    )
    targets = {
        target.host: target.target_id
        for target in runtime.target_service.list_targets(now=NOW)
    }
    first_target = targets["dev-a.example.test"]
    second_target = targets["dev-b.example.test"]
    asyncio.run(
        runtime.approvals.request(
            {"kind": "docker.restart", "target_id": second_target, "service": "shared-api"},
            now=NOW,
            target_id=second_target,
            service="shared-api",
        )
    )
    projection = ServiceProjectionService(
        target_service=runtime.target_service,
        target_store=runtime.target_store,
        projects=runtime.projects,
        approvals=runtime.approvals._approvals,
        investigations=runtime.investigation_store,
        operations=runtime.operation_store,
        logs=runtime.log_store,
        now=lambda: NOW,
        windows=ProjectionWindows(),
    )

    view = projection.read_service(
        "shared-api", allowed_target_ids=frozenset({first_target})
    )

    assert view is not None
    assert view.pending_approval_count == 0


def test_service_projection_finds_recent_errors_beyond_old_info_history(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _, _service_id = _seed(runtime)
    batch = []
    for index in range(1000):
        observed_at = NOW - timedelta(hours=2, minutes=index)
        batch.append(
            LogRecord(
                log_id=f"log-old-{index}",
                subscription_id=None,
                project_id="payments",
                target_id="dev-a",
                service_name="payment-api",
                source_kind=LogSourceKind.FILE,
                scope=LogScope.HOST,
                source_ref="/var/log/payments/api.log",
                cursor=f"offset:{index+10}",
                dedupe_key=hashlib.sha256(f"old-{index}".encode()).hexdigest(),
                observed_at=observed_at,
                event_time=None,
                severity=LogSeverity.INFO,
                message_redacted=f"INFO old line {index}",
                redaction_summary={},
                normal_signal=None,
                correlation_key=None,
                evidence_ref_id=None,
                created_at=observed_at,
            )
        )
    runtime.log_store.append_batch(tuple(batch))
    recent_error_at = NOW - timedelta(minutes=1)
    runtime.log_store.append_batch(
        (
            LogRecord(
                log_id="log-recent-error",
                subscription_id=None,
                project_id="payments",
                target_id="dev-a",
                service_name="payment-api",
                source_kind=LogSourceKind.FILE,
                scope=LogScope.HOST,
                source_ref="/var/log/payments/api.log",
                cursor="offset:9999",
                dedupe_key=hashlib.sha256(b"recent-error").hexdigest(),
                observed_at=recent_error_at,
                event_time=None,
                severity=LogSeverity.ERROR,
                message_redacted="ERROR recent outage",
                redaction_summary={},
                normal_signal=None,
                correlation_key=None,
                evidence_ref_id=None,
                created_at=recent_error_at,
            ),
        )
    )
    projection = ServiceProjectionService(
        target_service=runtime.target_service,
        target_store=runtime.target_store,
        projects=runtime.projects,
        approvals=runtime.approvals._approvals,
        investigations=runtime.investigation_store,
        operations=runtime.operation_store,
        logs=runtime.log_store,
        now=lambda: NOW,
        windows=ProjectionWindows(),
    )

    view = projection.read_service("payment-api")

    assert view is not None
    assert view.status is HealthStatus.DEGRADED
    assert view.last_observed_at == recent_error_at
