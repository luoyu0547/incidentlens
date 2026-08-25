from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from incidentlens_control_plane.changes.types import ChangeSetStatus, FileChange
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType
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
from incidentlens_control_plane.projections.investigations import InvestigationSummaryProjectionService
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
from incidentlens_control_plane.runtime import build_runtime

NOW = datetime(2026, 8, 25, 16, 0, tzinfo=UTC)


def _runtime(tmp_path: Path):
    return build_runtime(
        RuntimeSettings(data_dir=tmp_path / "runtime"),
        transport_factory=FakeTransportFactory(),
    )


def _register_target(runtime) -> str:
    from incidentlens_control_plane.project_registry.types import (
        ProjectRegistration,
        ServiceRegistration,
        TargetRegistration,
    )

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
                    container_names=("payment-api-1",),
                    allowed_host_paths=(PurePosixPath("/srv/payments"),),
                    protected_remote_paths=(PurePosixPath("/srv/payments/.env"),),
                ),
            ),
        ),
        now=NOW,
    )
    return runtime.target_service.get_target("dev-a", now=NOW).target_id


def _seed_investigation(runtime) -> tuple[str, str]:
    runtime.investigation_store.create_investigation(
        Investigation(
            investigation_id="inv-1",
            incident_id="inc-1",
            project_id="payments",
            target_id="dev-a",
            service="payment-api",
            symptom="checkout errors",
            status=InvestigationStatus.WAITING_APPROVAL,
            budget=InvestigationBudget(),
            usage=UsageCounters(),
            created_at=NOW - timedelta(minutes=15),
            updated_at=NOW,
        )
    )
    runtime.investigation_store.create_agent_run(
        AgentRun(
            agent_run_id="run-1",
            investigation_id="inv-1",
            parent_run_id=None,
            kind=AgentRunKind.PARENT,
            scope=AgentScope(
                project_id="payments",
                target_id="dev-a",
                scope=LogScope.HOST,
                allowed_host_paths=(PurePosixPath("/srv/payments"),),
            ),
            status=AgentRunStatus.WAITING_APPROVAL,
            budget=AgentBudget(),
            usage=UsageCounters(),
            created_at=NOW - timedelta(minutes=14),
            updated_at=NOW,
        )
    )
    runtime.investigation_store.create_hypothesis(
        Hypothesis(
            hypothesis_id="hyp-1",
            agent_run_id="run-1",
            summary="connection pool is too small",
            status=HypothesisStatus.ACTIVE,
            evidence_ids=(),
            created_at=NOW - timedelta(minutes=13),
            updated_at=NOW - timedelta(minutes=13),
        )
    )
    record = LogRecord(
        log_id="log-1",
        subscription_id=None,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payments/api.log",
        cursor="offset:7",
        dedupe_key=hashlib.sha256(b"log-1").hexdigest(),
        observed_at=NOW - timedelta(minutes=12),
        event_time=None,
        severity=LogSeverity.ERROR,
        message_redacted="ERROR token=[REDACTED_TOKEN] checkout failed",
        redaction_summary={"token": 1},
        normal_signal=None,
        correlation_key=None,
        evidence_ref_id=None,
        created_at=NOW - timedelta(minutes=12),
    )
    runtime.log_store.append_batch((record,))
    log_evidence = runtime.evidence.create_from_log_record(
        record,
        incident_id="inc-1",
        created_by="tester",
        now=NOW - timedelta(minutes=12),
        agent_run_id="run-1",
    )
    runtime.investigation_store.create_conclusion(
        agent_run_id="run-1",
        investigation_id="inv-1",
        conclusion=Conclusion(
            summary="connection pool saturation caused request failures",
            evidence_ids=(log_evidence.evidence_ref_id,),
        ),
        now=NOW - timedelta(minutes=11),
    )
    runtime.change_store.create_changeset(
        changeset_id="chs-1",
        incident_id="inc-1",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        files=(
            FileChange(
                file_change_id="fc-1",
                scope="host",
                remote_path="/srv/payments/app.py",
                expected_sha256="a" * 64,
                replacement_sha256="b" * 64,
                diff_text="@@\n-old\n+new\n",
            ),
        ),
        verification_plan="run health check",
        rollback_plan="restore last build",
        approval_id="apr-1",
    )
    for status in (
        ChangeSetStatus.PREFLIGHTED,
        ChangeSetStatus.LOCALLY_BACKED_UP,
        ChangeSetStatus.REMOTELY_BACKED_UP,
        ChangeSetStatus.APPLIED,
        ChangeSetStatus.VALIDATED,
    ):
        runtime.change_store.transition("chs-1", status)
    validation = runtime.evidence_service.record_validation_result(
        agent_run_id="run-1",
        incident_id="inc-1",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_ref="validator:health",
        validator="health",
        passed=False,
        detail="health endpoint returned 500",
        created_by="tester",
        now=NOW - timedelta(minutes=10),
    )
    approval = asyncio.run(
        runtime.approvals.request(
            {"kind": "docker.restart", "target_id": "dev-a", "service": "payment-api"},
            now=NOW - timedelta(minutes=9),
            target_id="dev-a",
            service="payment-api",
            investigation_id="inv-1",
            agent_run_id="run-1",
            tool_call_id="call-1",
        )
    )
    for sequence, (event_type, payload) in enumerate(
        (
            (
                RuntimeEventType.INVESTIGATION_CREATED,
                {
                    "investigation_id": "inv-1",
                    "target_id": "dev-a",
                    "status": "created",
                },
            ),
            (
                RuntimeEventType.INVESTIGATION_STARTED,
                {
                    "investigation_id": "inv-1",
                    "run_id": "run-1",
                    "status": "running",
                },
            ),
            (
                RuntimeEventType.INVESTIGATION_STATUS_CHANGED,
                {
                    "investigation_id": "inv-1",
                    "previous": "running",
                    "status": "waiting_approval",
                },
            ),
            (
                RuntimeEventType.CONCLUSION_CREATED,
                {
                    "investigation_id": "inv-1",
                    "run_id": "run-1",
                    "evidence_ids": [log_evidence.evidence_ref_id],
                    "conclusion": {
                        "summary": "connection pool saturation caused request failures",
                        "evidence_ids": [log_evidence.evidence_ref_id],
                    },
                },
            ),
        ),
        start=1,
    ):
        runtime.events.append(
            RuntimeEvent(
                event_id=f"evt-{sequence}",
                sequence=0,
                event_type=event_type,
                occurred_at=NOW - timedelta(minutes=20 - sequence),
                payload=payload,
            )
        )
    return approval.approval_id, validation.evidence_ref_id


def test_investigation_summary_projection_derives_milestones_and_safe_details(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    facade_target_id = _register_target(runtime)
    approval_id, validation_id = _seed_investigation(runtime)
    projection = InvestigationSummaryProjectionService(
        target_service=runtime.target_service,
        target_store=runtime.target_store,
        investigations=runtime.investigation_store,
        approvals=runtime.approvals._approvals,
        changes=runtime.change_store,
        evidence=runtime.evidence,
        logs=runtime.log_store,
        events=runtime.events,
        now=lambda: NOW,
    )

    summary = projection.get_summary("inv-1")

    assert summary is not None
    assert summary.issue_id == "iss_inv-1"
    assert summary.target_id == facade_target_id
    assert summary.pending_approval_ids == (approval_id,)
    assert summary.milestones[0].event_type is RuntimeEventType.INVESTIGATION_CREATED
    assert summary.milestones[-1].event_type is RuntimeEventType.CONCLUSION_CREATED
    assert summary.hypotheses[0].hypothesis_id == "hyp-1"
    assert summary.change_summaries[0].changeset_id == "chs-1"
    assert summary.verification_summaries[0].evidence_ref_id == validation_id
    assert summary.verification_summaries[0].passed is False
    assert summary.evidence[0].log_cursor is not None
    assert summary.evidence[0].log_cursor.startswith("lc1_")

    dumped = summary.model_dump_json()
    assert "/var/log/payments/api.log" not in dumped
    assert "content_sha256" not in dumped
    assert "metadata" not in dumped


def test_investigation_summary_projection_pages_filtered_milestones_to_terminal_event(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _register_target(runtime)
    _seed_investigation(runtime)
    for sequence in range(600):
        runtime.events.append(
            RuntimeEvent(
                event_id=f"evt-noise-{sequence}",
                sequence=0,
                event_type=RuntimeEventType.AGENT_TEXT_DELTA,
                occurred_at=NOW + timedelta(seconds=sequence),
                payload={
                    "investigation_id": "inv-1",
                    "session_id": "session-1",
                    "message_id": f"msg-{sequence}",
                    "run_id": "run-1",
                    "text": "noise",
                },
            )
        )
    runtime.events.append(
        RuntimeEvent(
            event_id="evt-terminal",
            sequence=0,
            event_type=RuntimeEventType.INVESTIGATION_FAILED,
            occurred_at=NOW + timedelta(minutes=20),
            payload={
                "investigation_id": "inv-1",
                "incident_id": "inc-1",
                "status": "failed",
            },
        )
    )
    projection = InvestigationSummaryProjectionService(
        target_service=runtime.target_service,
        target_store=runtime.target_store,
        investigations=runtime.investigation_store,
        approvals=runtime.approvals._approvals,
        changes=runtime.change_store,
        evidence=runtime.evidence,
        logs=runtime.log_store,
        events=runtime.events,
        now=lambda: NOW,
    )

    summary = projection.get_summary("inv-1")

    assert summary is not None
    assert summary.milestones[-1].event_type is RuntimeEventType.INVESTIGATION_FAILED
    assert all(
        item.event_type is not RuntimeEventType.AGENT_TEXT_DELTA
        for item in summary.milestones
    )
