from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from incidentlens_control_plane.changes.types import ChangeSetStatus, FileChange
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


def _create_run(
    runtime,
    *,
    investigation_id: str,
    run_id: str,
    target_id: str,
    project_id: str = "payments",
) -> None:
    runtime.investigation_store.create_agent_run(
        AgentRun(
            agent_run_id=run_id,
            investigation_id=investigation_id,
            parent_run_id=None,
            kind=AgentRunKind.PARENT,
            scope=AgentScope(
                project_id=project_id,
                target_id=target_id,
                scope=LogScope.HOST,
                allowed_host_paths=(PurePosixPath(f"/srv/{project_id}"),),
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
    approval_id: str | None = None,
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
        approval_id=approval_id,
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


def _seed_changeset_approval(
    runtime,
    *,
    investigation_id: str,
    run_id: str,
    project_id: str,
    target_id: str,
    service_name: str,
    changeset_id: str,
    created_at: datetime,
) -> str:
    approval = asyncio.run(
        runtime.approvals.request(
            {
                "kind": "file.write",
                "target_id": target_id,
                "service": service_name,
                "changeset_id": changeset_id,
            },
            now=created_at,
            project_id=project_id,
            target_id=target_id,
            service=service_name,
            investigation_id=investigation_id,
            agent_run_id=run_id,
            changeset_id=changeset_id,
        )
    )
    asyncio.run(
        runtime.approvals.approve(
            approval.approval_id,
            now=created_at + timedelta(seconds=1),
            actor="tester",
            reason="link changeset to investigation",
        )
    )
    return approval.approval_id


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
        approval_id=_seed_changeset_approval(
            runtime,
            investigation_id="inv-mitigated",
            run_id="run-mitigated",
            project_id="payments",
            target_id="dev-a",
            service_name="payment-api",
            changeset_id="chs-mitigated",
            created_at=NOW - timedelta(minutes=5, seconds=30),
        ),
    )
    _seed_changeset(
        runtime,
        incident_id="inc-resolved",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        changeset_id="chs-resolved",
        final_status=ChangeSetStatus.VERIFIED,
        approval_id=_seed_changeset_approval(
            runtime,
            investigation_id="inv-resolved",
            run_id="run-resolved",
            project_id="payments",
            target_id="dev-a",
            service_name="payment-api",
            changeset_id="chs-resolved",
            created_at=NOW - timedelta(minutes=4, seconds=30),
        ),
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


def test_issue_projection_scopes_changes_and_evidence_to_exact_ownership(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _register_target(
        runtime,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
    )
    _register_target(
        runtime,
        project_id="ops",
        target_id="dev-b",
        service_name="worker-api",
    )
    _create_investigation(
        runtime,
        investigation_id="inv-payments",
        incident_id="inc-shared",
        project_id="payments",
        registry_target_id="dev-a",
        service_name="payment-api",
        status=InvestigationStatus.RUNNING,
        created_at=NOW - timedelta(minutes=5),
    )
    _create_investigation(
        runtime,
        investigation_id="inv-ops",
        incident_id="inc-shared",
        project_id="ops",
        registry_target_id="dev-b",
        service_name="worker-api",
        status=InvestigationStatus.RUNNING,
        created_at=NOW - timedelta(minutes=4),
    )
    _create_run(
        runtime,
        investigation_id="inv-payments",
        run_id="run-payments",
        target_id="dev-a",
    )
    _create_run(
        runtime,
        investigation_id="inv-ops",
        run_id="run-ops",
        target_id="dev-b",
        project_id="ops",
    )
    _seed_changeset(
        runtime,
        incident_id="inc-shared",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        changeset_id="chs-payments",
        final_status=ChangeSetStatus.APPLIED,
        approval_id=_seed_changeset_approval(
            runtime,
            investigation_id="inv-payments",
            run_id="run-payments",
            project_id="payments",
            target_id="dev-a",
            service_name="payment-api",
            changeset_id="chs-payments",
            created_at=NOW - timedelta(minutes=3, seconds=30),
        ),
    )
    _seed_changeset(
        runtime,
        incident_id="inc-shared",
        project_id="ops",
        target_id="dev-b",
        service_name="worker-api",
        changeset_id="chs-ops",
        final_status=ChangeSetStatus.VERIFIED,
        approval_id=_seed_changeset_approval(
            runtime,
            investigation_id="inv-ops",
            run_id="run-ops",
            project_id="ops",
            target_id="dev-b",
            service_name="worker-api",
            changeset_id="chs-ops",
            created_at=NOW - timedelta(minutes=2, seconds=30),
        ),
    )
    payments_evidence = _seed_log_evidence(
        runtime,
        incident_id="inc-shared",
        run_id="run-payments",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        cursor="offset:11",
    )
    ops_evidence = _seed_log_evidence(
        runtime,
        incident_id="inc-shared",
        run_id="run-ops",
        project_id="ops",
        target_id="dev-b",
        service_name="worker-api",
        cursor="offset:22",
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

    payments_issue = projection.get_issue("iss_inv-payments")
    ops_issue = projection.get_issue("iss_inv-ops")

    assert payments_issue is not None
    assert ops_issue is not None
    assert payments_issue.resolution is not None
    assert payments_issue.resolution.changeset_id == "chs-payments"
    assert [item.evidence_ref_id for item in payments_issue.evidence] == [
        payments_evidence.evidence_ref_id
    ]
    assert ops_issue.resolution is not None
    assert ops_issue.resolution.changeset_id == "chs-ops"
    assert [item.evidence_ref_id for item in ops_issue.evidence] == [
        ops_evidence.evidence_ref_id
    ]


def test_issue_projection_does_not_resolve_on_failed_or_inconclusive_validation(
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
        investigation_id="inv-failed-validation",
        incident_id="inc-failed-validation",
        project_id="payments",
        registry_target_id="dev-a",
        service_name="payment-api",
        status=InvestigationStatus.RUNNING,
        created_at=NOW - timedelta(minutes=5),
    )
    _create_run(
        runtime,
        investigation_id="inv-failed-validation",
        run_id="run-failed-validation",
        target_id="dev-a",
    )
    _seed_changeset(
        runtime,
        incident_id="inc-failed-validation",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        changeset_id="chs-failed-validation",
        final_status=ChangeSetStatus.VERIFIED,
        approval_id=_seed_changeset_approval(
            runtime,
            investigation_id="inv-failed-validation",
            run_id="run-failed-validation",
            project_id="payments",
            target_id="dev-a",
            service_name="payment-api",
            changeset_id="chs-failed-validation",
            created_at=NOW - timedelta(minutes=4, seconds=30),
        ),
    )
    failed_validation = _seed_validation_evidence(
        runtime,
        incident_id="inc-failed-validation",
        run_id="run-failed-validation",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        passed=False,
    )
    runtime.evidence.create(
        EvidenceRef(
            evidence_ref_id="ev-inconclusive",
            incident_id="inc-failed-validation",
            evidence_kind=EvidenceKind.VALIDATION_RESULT,
            agent_run_id="run-failed-validation",
            project_id="payments",
            target_id="dev-a",
            service_name="payment-api",
            source_ref="validator:health",
            content_redacted="health endpoint stayed ambiguous",
            content_sha256=hashlib.sha256(
                b"health endpoint stayed ambiguous"
            ).hexdigest(),
            redaction_summary={},
            truncation=None,
            metadata={"validator": "health"},
            created_at=NOW + timedelta(minutes=1),
            created_by="tester",
        )
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

    issue = projection.get_issue("iss_inv-failed-validation")

    assert issue is not None
    assert issue.status is IssueStatus.MITIGATED
    assert issue.verification is not None
    assert issue.verification.evidence_ref_id == "ev-inconclusive"
    assert issue.verification.passed is None

    runtime.evidence.get(failed_validation.evidence_ref_id)


def test_issue_projection_omits_sibling_and_legacy_artifacts_for_shared_scope_investigations(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _register_target(
        runtime,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
    )
    for investigation_id, created_at in (
        ("inv-alpha", NOW - timedelta(minutes=6)),
        ("inv-bravo", NOW - timedelta(minutes=5)),
    ):
        _create_investigation(
            runtime,
            investigation_id=investigation_id,
            incident_id="inc-shared-scope",
            project_id="payments",
            registry_target_id="dev-a",
            service_name="payment-api",
            status=InvestigationStatus.RUNNING,
            created_at=created_at,
        )
    _create_run(runtime, investigation_id="inv-alpha", run_id="run-alpha", target_id="dev-a")
    _create_run(runtime, investigation_id="inv-bravo", run_id="run-bravo", target_id="dev-a")

    alpha_log = _seed_log_evidence(
        runtime,
        incident_id="inc-shared-scope",
        run_id="run-alpha",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        cursor="offset:31",
    )
    bravo_log = _seed_log_evidence(
        runtime,
        incident_id="inc-shared-scope",
        run_id="run-bravo",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        cursor="offset:32",
        severity=LogSeverity.CRITICAL,
    )
    _seed_changeset(
        runtime,
        incident_id="inc-shared-scope",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        changeset_id="chs-legacy-shared",
        final_status=ChangeSetStatus.VERIFIED,
    )
    _seed_changeset(
        runtime,
        incident_id="inc-shared-scope",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        changeset_id="chs-bravo",
        final_status=ChangeSetStatus.VERIFIED,
        approval_id=_seed_changeset_approval(
            runtime,
            investigation_id="inv-bravo",
            run_id="run-bravo",
            project_id="payments",
            target_id="dev-a",
            service_name="payment-api",
            changeset_id="chs-bravo",
            created_at=NOW - timedelta(minutes=4, seconds=30),
        ),
    )
    _seed_validation_evidence(
        runtime,
        incident_id="inc-shared-scope",
        run_id="run-bravo",
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

    alpha_issue = projection.get_issue("iss_inv-alpha")
    bravo_issue = projection.get_issue("iss_inv-bravo")

    assert alpha_issue is not None
    assert alpha_issue.status is IssueStatus.OPEN
    assert alpha_issue.resolution is None
    assert alpha_issue.verification is None
    assert alpha_issue.severity is LogSeverity.ERROR
    assert [item.evidence_ref_id for item in alpha_issue.evidence] == [
        alpha_log.evidence_ref_id
    ]

    assert bravo_issue is not None
    assert bravo_issue.status is IssueStatus.RESOLVED
    assert bravo_issue.resolution is not None
    assert bravo_issue.resolution.changeset_id == "chs-bravo"
    assert bravo_issue.verification is not None
    assert bravo_issue.verification.passed is True
    assert bravo_issue.severity is LogSeverity.CRITICAL
    assert {item.evidence_ref_id for item in bravo_issue.evidence} == {
        bravo_log.evidence_ref_id,
        bravo_issue.verification.evidence_ref_id,
    }
