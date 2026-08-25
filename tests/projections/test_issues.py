from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from incidentlens_control_plane.changes.types import ChangeSetStatus, FileChange
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.evidence.types import EvidenceKind
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
    Hypothesis,
    HypothesisStatus,
    Investigation,
    InvestigationBudget,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogRecord, LogScope, LogSeverity, LogSourceKind
from incidentlens_control_plane.projections.issues import IssueProjectionService, IssueStatus
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
from incidentlens_control_plane.runtime import build_runtime

NOW = datetime(2026, 8, 25, 15, 0, tzinfo=UTC)


def _runtime(tmp_path: Path):
    return build_runtime(
        RuntimeSettings(data_dir=tmp_path / "runtime"),
        transport_factory=FakeTransportFactory(),
    )


def _register_target(runtime, *, project_id: str, target_id: str, service_name: str) -> str:
    from incidentlens_control_plane.project_registry.types import (
        ProjectRegistration,
        ServiceRegistration,
        TargetRegistration,
    )

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


def _create_investigation(
    runtime,
    *,
    investigation_id: str,
    incident_id: str,
    project_id: str,
    registry_target_id: str,
    service_name: str,
    status: InvestigationStatus,
    created_at: datetime,
    updated_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> None:
    runtime.investigation_store.create_investigation(
        Investigation(
            investigation_id=investigation_id,
            incident_id=incident_id,
            project_id=project_id,
            target_id=registry_target_id,
            service=service_name,
            symptom=f"{service_name} symptom",
            status=status,
            budget=InvestigationBudget(),
            usage=UsageCounters(),
            created_at=created_at,
            updated_at=updated_at or created_at,
            completed_at=completed_at,
        )
    )


def _create_run(runtime, *, investigation_id: str, run_id: str, target_id: str) -> None:
    runtime.investigation_store.create_agent_run(
        AgentRun(
            agent_run_id=run_id,
            investigation_id=investigation_id,
            parent_run_id=None,
            kind=AgentRunKind.PARENT,
            scope=AgentScope(
                project_id="payments",
                target_id=target_id,
                scope=LogScope.HOST,
                allowed_host_paths=(PurePosixPath("/srv/payments"),),
            ),
            status=AgentRunStatus.COMPLETED,
            budget=AgentBudget(),
            usage=UsageCounters(),
            created_at=NOW,
            updated_at=NOW,
            completed_at=NOW,
        )
    )


def _seed_log_evidence(
    runtime,
    *,
    incident_id: str,
    run_id: str,
    project_id: str,
    target_id: str,
    service_name: str,
    severity: LogSeverity = LogSeverity.ERROR,
    cursor: str = "offset:42",
):
    record = LogRecord(
        log_id=f"log-{run_id}",
        subscription_id=None,
        project_id=project_id,
        target_id=target_id,
        service_name=service_name,
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payments/api.log",
        cursor=cursor,
        dedupe_key=hashlib.sha256(f"{run_id}:{cursor}".encode("utf-8")).hexdigest(),
        observed_at=NOW - timedelta(minutes=1),
        event_time=None,
        severity=severity,
        message_redacted="ERROR token=[REDACTED_TOKEN] checkout failed",
        redaction_summary={"token": 1},
        normal_signal=None,
        correlation_key=None,
        evidence_ref_id=None,
        created_at=NOW - timedelta(minutes=1),
    )
    runtime.log_store.append_batch((record,))
    return runtime.evidence.create_from_log_record(
        record,
        incident_id=incident_id,
        created_by="tester",
        now=NOW,
        agent_run_id=run_id,
    )


def _seed_validation_evidence(
    runtime,
    *,
    incident_id: str,
    run_id: str,
    project_id: str,
    target_id: str,
    service_name: str,
    passed: bool,
):
    return runtime.evidence_service.record_validation_result(
        agent_run_id=run_id,
        incident_id=incident_id,
        project_id=project_id,
        target_id=target_id,
        service_name=service_name,
        source_ref="validator:health",
        validator="health",
        passed=passed,
        detail="health endpoint returned 200" if passed else "health endpoint returned 500",
        created_by="tester",
        now=NOW,
    )


def _seed_changeset(
    runtime,
    *,
    incident_id: str,
    project_id: str,
    target_id: str,
    service_name: str,
    changeset_id: str,
    final_status: ChangeSetStatus,
) -> None:
    runtime.change_store.create_changeset(
        changeset_id=changeset_id,
        incident_id=incident_id,
        project_id=project_id,
        target_id=target_id,
        service_name=service_name,
        files=(
            FileChange(
                file_change_id=f"fc-{changeset_id}",
                scope="host",
                remote_path="/srv/payments/app.py",
                expected_sha256="a" * 64,
                replacement_sha256="b" * 64,
                diff_text="@@\n-old\n+new\n",
            ),
        ),
        verification_plan="run health check",
        rollback_plan="restore previous build",
    )
    sequence = (
        ChangeSetStatus.PREFLIGHTED,
        ChangeSetStatus.LOCALLY_BACKED_UP,
        ChangeSetStatus.REMOTELY_BACKED_UP,
        ChangeSetStatus.APPLIED,
        ChangeSetStatus.VALIDATED,
        ChangeSetStatus.VERIFIED,
    )
    for status in sequence:
        runtime.change_store.transition(changeset_id, status)
        if status is final_status:
            break


def _seed_hypothesis(
    runtime,
    *,
    run_id: str,
    hypothesis_id: str,
    summary: str,
    status: HypothesisStatus = HypothesisStatus.PROPOSED,
) -> None:
    runtime.investigation_store.create_hypothesis(
        Hypothesis(
            hypothesis_id=hypothesis_id,
            agent_run_id=run_id,
            summary=summary,
            status=status,
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _seed_conclusion(
    runtime,
    *,
    run_id: str,
    investigation_id: str,
    summary: str,
    evidence_ids: tuple[str, ...],
) -> None:
    runtime.investigation_store.create_conclusion(
        agent_run_id=run_id,
        investigation_id=investigation_id,
        conclusion=Conclusion(summary=summary, evidence_ids=evidence_ids),
        now=NOW,
    )


def test_issue_projection_uses_investigation_ids_and_never_creates_issues_table(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    facade_target_id = _register_target(
        runtime,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
    )
    _create_investigation(
        runtime,
        investigation_id="inv-1",
        incident_id="inc-1",
        project_id="payments",
        registry_target_id="dev-a",
        service_name="payment-api",
        status=InvestigationStatus.RUNNING,
        created_at=NOW - timedelta(minutes=10),
    )
    projection = IssueProjectionService(
        target_service=runtime.target_service,
        target_store=runtime.target_store,
        investigations=runtime.investigation_store,
        approvals=runtime.approvals._approvals,
        changes=runtime.change_store,
        evidence=runtime.evidence,
        logs=runtime.log_store,
        now=lambda: NOW,
    )

    page = projection.list_issues()
    view = projection.get_issue("iss_inv-1")

    assert page.items[0].issue_id == "iss_inv-1"
    assert view is not None
    assert view.issue_id == "iss_inv-1"
    assert view.investigation_id == "inv-1"
    assert view.target_id == facade_target_id
    assert view.root_cause_confidence is None

    with sqlite3.connect(tmp_path / "runtime" / "runtime.db") as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "issues" not in tables


def test_issue_projection_maps_statuses_and_requires_grounded_conclusion(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _register_target(
        runtime,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
    )

    _create_investigation(
        runtime,
        investigation_id="inv-wait",
        incident_id="inc-wait",
        project_id="payments",
        registry_target_id="dev-a",
        service_name="payment-api",
        status=InvestigationStatus.WAITING_APPROVAL,
        created_at=NOW - timedelta(minutes=9),
    )
    _create_investigation(
        runtime,
        investigation_id="inv-mitigated",
        incident_id="inc-mitigated",
        project_id="payments",
        registry_target_id="dev-a",
        service_name="payment-api",
        status=InvestigationStatus.RUNNING,
        created_at=NOW - timedelta(minutes=8),
    )
    _create_investigation(
        runtime,
        investigation_id="inv-resolved",
        incident_id="inc-resolved",
        project_id="payments",
        registry_target_id="dev-a",
        service_name="payment-api",
        status=InvestigationStatus.COMPLETED,
        created_at=NOW - timedelta(minutes=7),
        updated_at=NOW,
        completed_at=NOW,
    )
    _create_investigation(
        runtime,
        investigation_id="inv-failed",
        incident_id="inc-failed",
        project_id="payments",
        registry_target_id="dev-a",
        service_name="payment-api",
        status=InvestigationStatus.FAILED,
        created_at=NOW - timedelta(minutes=6),
        updated_at=NOW,
    )

    _create_run(runtime, investigation_id="inv-resolved", run_id="run-resolved", target_id="dev-a")
    _create_run(
        runtime,
        investigation_id="inv-mitigated",
        run_id="run-mitigated",
        target_id="dev-a",
    )
    _create_run(runtime, investigation_id="inv-failed", run_id="run-failed", target_id="dev-a")

    grounded_log = _seed_log_evidence(
        runtime,
        incident_id="inc-resolved",
        run_id="run-resolved",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        severity=LogSeverity.CRITICAL,
    )
    _seed_conclusion(
        runtime,
        run_id="run-resolved",
        investigation_id="inv-resolved",
        summary="misconfigured feature flag exhausted the pool",
        evidence_ids=(grounded_log.evidence_ref_id,),
    )
    _seed_conclusion(
        runtime,
        run_id="run-failed",
        investigation_id="inv-failed",
        summary="ungrounded guess should not surface",
        evidence_ids=(),
    )
    _seed_hypothesis(
        runtime,
        run_id="run-resolved",
        hypothesis_id="hyp-1",
        summary="feature flag rollout regressed connection limits",
        status=HypothesisStatus.CONFIRMED,
    )
    _seed_changeset(
        runtime,
        incident_id="inc-mitigated",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        changeset_id="chs-mitigated",
        final_status=ChangeSetStatus.APPLIED,
    )
    _seed_changeset(
        runtime,
        incident_id="inc-resolved",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        changeset_id="chs-resolved",
        final_status=ChangeSetStatus.VERIFIED,
    )
    _seed_validation_evidence(
        runtime,
        incident_id="inc-resolved",
        run_id="run-resolved",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        passed=True,
    )

    projection = IssueProjectionService(
        target_service=runtime.target_service,
        target_store=runtime.target_store,
        investigations=runtime.investigation_store,
        approvals=runtime.approvals._approvals,
        changes=runtime.change_store,
        evidence=runtime.evidence,
        logs=runtime.log_store,
        now=lambda: NOW,
    )

    waiting = projection.get_issue("iss_inv-wait")
    mitigated = projection.get_issue("iss_inv-mitigated")
    resolved = projection.get_issue("iss_inv-resolved")
    failed = projection.get_issue("iss_inv-failed")

    assert waiting is not None and waiting.status is IssueStatus.WAITING_APPROVAL
    assert mitigated is not None and mitigated.status is IssueStatus.MITIGATED
    assert resolved is not None and resolved.status is IssueStatus.RESOLVED
    assert failed is not None and failed.status is IssueStatus.FAILED

    assert resolved.severity is LogSeverity.CRITICAL
    assert resolved.root_cause == "misconfigured feature flag exhausted the pool"
    assert resolved.root_cause_confidence is None
    assert resolved.resolution is not None
    assert resolved.resolution.changeset_id == "chs-resolved"
    assert resolved.verification is not None
    assert resolved.verification.passed is True

    assert failed.root_cause is None
    assert failed.root_cause_confidence is None
    assert "content_sha256" not in resolved.model_dump_json()
    assert "metadata" not in resolved.model_dump_json()

