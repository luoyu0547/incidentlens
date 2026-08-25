"""Tests for startup recovery and orderly shutdown of the agent runtime.

The harness reuses the real SQLite-backed runtime (project registry, log/
evidence/approval stores, gateway, sessions) and the scripted FakeProvider, so
every recovery scenario walks the real store transitions, evidence pipeline and
approval paths.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from incidentlens_control_plane.approvals.types import ApprovalDownstreamStatus
from incidentlens_control_plane.evidence.types import EvidenceKind
from incidentlens_control_plane.investigation.fake_provider import (
    FakeProvider,
    FakeProviderRegistry,
    RequestToolsStep,
    StopStep,
)
from incidentlens_control_plane.investigation.orchestrator import AgentOrchestrator
from incidentlens_control_plane.investigation.provider import StopSignal
from incidentlens_control_plane.investigation.recovery import RecoveryService
from incidentlens_control_plane.investigation.service import (
    InvestigationService,
    NotAcceptingInvestigations,
    TooManyActiveInvestigations,
)
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    ChildReport,
    ChildReportReceipt,
    ChildReportStatus,
    Investigation,
    InvestigationBudget,
    StopReason,
    ToolCall,
    UsageCounters,
)

from investigation.test_orchestrator import GroundedStopProvider
from investigation.test_tool_executor import (
    PROJECT_ID,
    SERVICE,
    TARGET_ID,
    HarnessTransportFactory,
    build_harness,
    make_scope,
    tool_request,
)

NOW = datetime(2026, 8, 12, 10, 0, 0, tzinfo=UTC)


def _make_investigation(
    harness: Any,
    *,
    investigation_id: str = "inv-1",
    status: InvestigationStatus = InvestigationStatus.RUNNING,
) -> Investigation:
    investigation = Investigation(
        investigation_id=investigation_id,
        incident_id="inc-1",
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        service=SERVICE,
        symptom="checkout requests are failing",
        status=status,
        budget=InvestigationBudget(),
        usage=UsageCounters(),
        created_at=NOW,
        updated_at=NOW,
    )
    harness.investigations.create_investigation(investigation)
    return investigation


def _make_run(
    harness: Any,
    *,
    run_id: str = "run-1",
    investigation_id: str = "inv-1",
    status: AgentRunStatus = AgentRunStatus.RUNNING,
    scope: AgentScope | None = None,
) -> AgentRun:
    run = AgentRun(
        agent_run_id=run_id,
        investigation_id=investigation_id,
        parent_run_id=None,
        kind=AgentRunKind.PARENT,
        scope=scope or make_scope(),
        status=status,
        budget=AgentBudget(),
        usage=UsageCounters(),
        created_at=NOW,
        updated_at=NOW,
    )
    harness.investigations.create_agent_run(run)
    return run


def _make_tool_call(
    harness: Any,
    *,
    tool_call_id: str = "call-1",
    run_id: str = "run-1",
    tool_name: str = "shell_exec",
    status: ToolCallStatus = ToolCallStatus.RUNNING,
) -> ToolCall:
    call = ToolCall(
        tool_call_id=tool_call_id,
        agent_run_id=run_id,
        tool_name=tool_name,
        status=status,
        idempotency_key=tool_call_id,
        planned_at=NOW,
    )
    harness.investigations.create_tool_call(call)
    return call


def build_recovery(
    harness: Any,
    registry: FakeProviderRegistry,
    *,
    now=None,
    shutdown_grace_seconds: float = 0.5,
    max_active_investigations: int = 8,
) -> tuple[AgentOrchestrator, InvestigationService, RecoveryService]:
    provider = FakeProvider(registry)
    wrapped = GroundedStopProvider(provider)
    now = now or (lambda: NOW)
    orchestrator = AgentOrchestrator(
        store=harness.investigations,
        provider=wrapped,
        executor=harness.executor,
        evidence=harness.evidence,
        projects=harness.projects,
        sessions=harness.sessions,
        now=now,
        global_child_limit=8,
    )
    service = InvestigationService(
        store=harness.investigations,
        orchestrator=orchestrator,
        now=now,
        approvals=harness.approvals,
        executor=harness.executor,
        max_active_investigations=max_active_investigations,
    )
    recovery = RecoveryService(
        store=harness.investigations,
        investigations=service,
        orchestrator=orchestrator,
        evidence=harness.evidence,
        approvals=harness.approvals,
        shutdown_grace_seconds=shutdown_grace_seconds,
        now=now,
    )
    return orchestrator, service, recovery


# ---------------------------------------------------------------------------
# Startup: durable child receipt reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_reconciles_terminal_child_receipt_exactly_once(
    tmp_path: Any,
) -> None:
    """Startup repairs a durable child receipt without invoking the provider."""
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.COMPLETED)
    _make_run(harness, status=AgentRunStatus.COMPLETED)
    child = AgentRun(
        agent_run_id="child-1",
        investigation_id="inv-1",
        parent_run_id="run-1",
        kind=AgentRunKind.CHILD,
        scope=make_scope(),
        status=AgentRunStatus.COMPLETED,
        budget=AgentBudget(),
        usage=UsageCounters(),
        created_at=NOW,
        updated_at=NOW,
    )
    harness.investigations.create_agent_run(child)
    evidence = harness.evidence.record_child_report(
        agent_run_id="child-1",
        incident_id="inc-1",
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        service_name=SERVICE,
        source_ref="child:child-1",
        report_summary="child found the failed dependency",
        child_run_id="child-1",
        parent_run_id="run-1",
        status=ChildReportStatus.COMPLETE.value,
        stop_reason=StopReason.COMPLETED.value,
        created_by="test",
        now=NOW,
    )
    report = ChildReport(
        agent_run_id="child-1",
        parent_run_id="run-1",
        status=ChildReportStatus.COMPLETE,
        summary="child found the failed dependency",
        findings=("dependency unavailable",),
        stop_reason=StopReason.COMPLETED,
        evidence_ids=(evidence.evidence_ref_id,),
        created_at=NOW,
    )
    harness.investigations.put_child_report_receipt(
        ChildReportReceipt(
            child_run_id="child-1",
            parent_run_id="run-1",
            report=report,
            evidence_id=evidence.evidence_ref_id,
            created_at=NOW,
        )
    )
    _, _, recovery = build_recovery(harness, registry)

    first = await recovery.startup()
    second = await recovery.startup()

    parent = harness.investigations.get_agent_run("run-1")
    notifications = [
        message
        for message in harness.investigations.list_transcript_messages("run-1")
        if any(
            getattr(block, "text", "").startswith("Child report child-1")
            for block in message.blocks
        )
    ]
    assert first.reconciled_child_receipts == 1
    assert second.reconciled_child_receipts == 0
    assert len(notifications) == 1
    assert [ref.operation_id for ref in parent.evidence].count("child:child-1") == 1
    assert harness.investigations.get_child_report_receipt("child-1").delivered_at is not None
    assert registry.requests("child-1") == ()


@pytest.mark.asyncio
async def test_startup_isolates_failed_child_receipt_parent(tmp_path: Any) -> None:
    """One invalid parent's receipt does not block another parent's delivery."""
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    for inv_id, parent_id in (("inv-1", "run-1"), ("inv-2", "run-2")):
        investigation = _make_investigation(
            harness, investigation_id=inv_id, status=InvestigationStatus.COMPLETED
        )
        parent = AgentRun(
            agent_run_id=parent_id,
            investigation_id=investigation.investigation_id,
            parent_run_id=None,
            kind=AgentRunKind.PARENT,
            scope=make_scope(),
            status=AgentRunStatus.COMPLETED,
            budget=AgentBudget(),
            usage=UsageCounters(),
            created_at=NOW,
            updated_at=NOW,
        )
        harness.investigations.create_agent_run(parent)
    child = AgentRun(
        agent_run_id="child-bad",
        investigation_id="inv-1",
        parent_run_id="run-1",
        kind=AgentRunKind.CHILD,
        scope=make_scope(),
        status=AgentRunStatus.COMPLETED,
        budget=AgentBudget(),
        usage=UsageCounters(),
        created_at=NOW,
        updated_at=NOW,
    )
    harness.investigations.create_agent_run(child)
    bad_report = ChildReport(
        agent_run_id="child-bad", parent_run_id="run-1",
        status=ChildReportStatus.COMPLETE, summary="bad receipt",
        findings=(), stop_reason=StopReason.COMPLETED, created_at=NOW,
    )
    harness.investigations.put_child_report_receipt(
        ChildReportReceipt(
            child_run_id="child-bad", parent_run_id="run-1", report=bad_report,
            evidence_id="missing-evidence", created_at=NOW,
        )
    )
    good_child = AgentRun(
        agent_run_id="child-good", investigation_id="inv-2", parent_run_id="run-2",
        kind=AgentRunKind.CHILD, scope=make_scope(), status=AgentRunStatus.COMPLETED,
        budget=AgentBudget(), usage=UsageCounters(), created_at=NOW, updated_at=NOW,
    )
    harness.investigations.create_agent_run(good_child)
    good_evidence = harness.evidence.record_child_report(
        agent_run_id="child-good", incident_id="inc-1", project_id=PROJECT_ID,
        target_id=TARGET_ID, service_name=SERVICE, source_ref="child:child-good",
        report_summary="good report", child_run_id="child-good", parent_run_id="run-2",
        status=ChildReportStatus.COMPLETE.value, stop_reason=StopReason.COMPLETED.value,
        created_by="test", now=NOW,
    )
    good_report = ChildReport(
        agent_run_id="child-good", parent_run_id="run-2", status=ChildReportStatus.COMPLETE,
        summary="good report", findings=(), stop_reason=StopReason.COMPLETED,
        created_at=NOW,
    )
    harness.investigations.put_child_report_receipt(
        ChildReportReceipt(
            child_run_id="child-good", parent_run_id="run-2", report=good_report,
            evidence_id=good_evidence.evidence_ref_id, created_at=NOW,
        )
    )
    _, _, recovery = build_recovery(harness, registry)

    summary = await recovery.startup()

    assert summary.reconciled_child_receipts == 1
    assert harness.investigations.get_child_report_receipt("child-bad").delivered_at is None
    assert harness.investigations.get_child_report_receipt("child-good").delivered_at is not None




@pytest.mark.asyncio
async def test_startup_reconciles_decided_approval_and_resumes_run(tmp_path: Any) -> None:
    """An approval decided before the restart re-executes its tool and resumes."""
    harness = build_harness(
        tmp_path,
        transport_factory=HarnessTransportFactory(
            shell_output=b"restarted", shell_status=0
        ),
    )
    registry = FakeProviderRegistry()
    _make_investigation(harness)
    _make_run(harness)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    tool_request(
                        "shell_exec",
                        tool_call_id="call-1",
                        service_name=SERVICE,
                        command="systemctl restart mysql",
                    ),
                )
            ),
            StopStep(
                stop_signal=StopSignal(
                    stop_reason=StopReason.COMPLETED, summary="investigation complete"
                )
            ),
        ],
    )
    _, service, recovery = build_recovery(harness, registry)

    # Run the loop once: the mutating shell tool parks the run on approval.
    parked = await service.resume_run("run-1")
    assert parked.status is AgentRunStatus.WAITING_APPROVAL
    pending = harness.approvals.list()
    assert len(pending) == 1 and pending[0].status.value == "pending"

    # The operator approves while the process is "down"; restart reconciles it.
    await harness.approvals.approve(pending[0].approval_id)

    summary = await recovery.startup()

    assert summary.reconciled_approvals == 1
    tool_call = harness.investigations.get_tool_call_by_provider_id("run-1", "call-1")
    assert tool_call.status is ToolCallStatus.SUCCEEDED
    assert service.get_run("run-1").status is AgentRunStatus.COMPLETED
    assert service.get_investigation("inv-1").status is InvestigationStatus.COMPLETED


@pytest.mark.asyncio
async def test_startup_reconciles_processing_approval_and_resumes_run(
    tmp_path: Any,
) -> None:
    """A decided approval left mid-processing is resumed on startup."""
    harness = build_harness(
        tmp_path,
        transport_factory=HarnessTransportFactory(
            shell_output=b"restarted", shell_status=0
        ),
    )
    registry = FakeProviderRegistry()
    _make_investigation(harness)
    _make_run(harness)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    tool_request(
                        "shell_exec",
                        tool_call_id="call-1",
                        service_name=SERVICE,
                        command="systemctl restart mysql",
                    ),
                )
            ),
            StopStep(
                stop_signal=StopSignal(
                    stop_reason=StopReason.COMPLETED, summary="investigation complete"
                )
            ),
        ],
    )
    _, service, recovery = build_recovery(harness, registry)

    parked = await service.resume_run("run-1")
    assert parked.status is AgentRunStatus.WAITING_APPROVAL
    pending = harness.approvals.list()
    assert len(pending) == 1 and pending[0].status.value == "pending"

    approved = await harness.approvals.approve(pending[0].approval_id)
    harness.approvals.mark_downstream(
        approved.approval_id,
        ApprovalDownstreamStatus.PROCESSING,
        now=NOW,
    )

    summary = await recovery.startup()

    assert summary.reconciled_approvals == 1
    tool_call = harness.investigations.get_tool_call_by_provider_id("run-1", "call-1")
    assert tool_call.status is ToolCallStatus.SUCCEEDED
    assert service.get_run("run-1").status is AgentRunStatus.COMPLETED
    assert service.get_investigation("inv-1").status is InvestigationStatus.COMPLETED


@pytest.mark.asyncio
async def test_startup_leaves_pending_approvals_for_the_operator(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness)
    _make_run(harness)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    tool_request(
                        "shell_exec",
                        tool_call_id="call-1",
                        service_name=SERVICE,
                        command="systemctl restart mysql",
                    ),
                )
            )
        ],
    )
    _, service, recovery = build_recovery(harness, registry)
    parked = await service.resume_run("run-1")
    assert parked.status is AgentRunStatus.WAITING_APPROVAL
    pending = harness.approvals.list()
    assert len(pending) == 1 and pending[0].status.value == "pending"

    summary = await recovery.startup()

    assert summary.reconciled_approvals == 0
    assert service.get_run("run-1").status is AgentRunStatus.WAITING_APPROVAL
    assert (
        harness.investigations.get_tool_call_by_provider_id("run-1", "call-1").status
        is ToolCallStatus.WAITING_APPROVAL
    )


# ---------------------------------------------------------------------------
# Startup: classification of in-flight runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_parks_dangerous_in_flight_run_uncertain_and_never_replays(
    tmp_path: Any,
) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness)
    _make_run(harness)
    _make_tool_call(harness, tool_call_id="call-1", tool_name="shell_exec")

    _, service, recovery = build_recovery(harness, registry)
    summary = await recovery.startup()

    assert summary.dangerous_parked == 1
    assert service.get_run("run-1").status is AgentRunStatus.PAUSED_UNCERTAIN_STATE
    assert (
        service.get_investigation("inv-1").status
        is InvestigationStatus.PAUSED_UNCERTAIN_STATE
    )
    tool_call = harness.investigations.get_tool_call_by_provider_id("run-1", "call-1")
    assert tool_call.status is ToolCallStatus.UNCERTAIN
    assert "never replayed" in tool_call.error_redacted
    # UNCERTAIN_STATE evidence was recorded for the audit trail.
    stored = harness.evidence_store.query(agent_run_id="run-1")
    assert any(item.evidence_kind is EvidenceKind.UNCERTAIN_STATE for item in stored)


@pytest.mark.asyncio
async def test_startup_marks_safe_in_flight_call_failed_and_keeps_run_resumable(
    tmp_path: Any,
) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness)
    _make_run(harness)
    _make_tool_call(harness, tool_call_id="call-1", tool_name="log_query")

    _, service, recovery = build_recovery(harness, registry)
    summary = await recovery.startup()

    assert summary.safe_repaired == 1
    assert summary.dangerous_parked == 0
    # A read-only in-flight call is retryable: the run stays RUNNING.
    assert service.get_run("run-1").status is AgentRunStatus.RUNNING
    tool_call = harness.investigations.get_tool_call_by_provider_id("run-1", "call-1")
    assert tool_call.status is ToolCallStatus.FAILED
    assert "retryable" in tool_call.error_redacted


@pytest.mark.asyncio
async def test_startup_finalises_cancel_requested_run_after_crash_mid_cancel(
    tmp_path: Any,
) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.CANCEL_REQUESTED)
    _make_run(harness, status=AgentRunStatus.CANCEL_REQUESTED)

    _, service, recovery = build_recovery(harness, registry)
    summary = await recovery.startup()

    assert summary.cancel_finalised == 1
    assert service.get_run("run-1").status is AgentRunStatus.CANCELLED
    assert service.get_investigation("inv-1").status is InvestigationStatus.CANCELLED


@pytest.mark.asyncio
async def test_startup_leaves_paused_run_parked_for_operator_resume(tmp_path: Any) -> None:
    """Nothing beyond decided approvals is auto-resumed on startup."""
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.PAUSED_BUDGET)
    _make_run(harness, status=AgentRunStatus.PAUSED_BUDGET)

    _, service, recovery = build_recovery(harness, registry)
    summary = await recovery.startup()

    assert summary.scanned_investigations == 1
    assert summary.dangerous_parked == 0
    assert service.get_run("run-1").status is AgentRunStatus.PAUSED_BUDGET
    assert service.get_investigation("inv-1").status is InvestigationStatus.PAUSED_BUDGET


# ---------------------------------------------------------------------------
# Shutdown: orderly teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_refuses_new_investigations_and_cancels_active(
    tmp_path: Any,
) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness)
    _make_run(harness)
    _make_tool_call(harness, tool_call_id="call-1", tool_name="shell_exec")

    _, service, recovery = build_recovery(harness, registry)
    cancelled = await recovery.shutdown()

    assert cancelled == 1
    assert recovery.accepting is False
    assert service.accepting is False
    assert service.get_investigation("inv-1").status is InvestigationStatus.CANCELLED
    assert service.get_run("run-1").status is AgentRunStatus.CANCELLED
    with pytest.raises(NotAcceptingInvestigations):
        service.create_investigation(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            symptom="late",
        )


@pytest.mark.asyncio
async def test_shutdown_sweeps_in_flight_calls_to_terminal(tmp_path: Any) -> None:
    """Shutdown leaves no unclassifiable running tool call behind."""
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness)
    _make_run(harness)
    # Dangerous call in flight, plus calls that were never going to run.
    _make_tool_call(harness, tool_call_id="call-danger", tool_name="file_write")
    _make_tool_call(
        harness, tool_call_id="call-waiting", tool_name="shell_exec",
        status=ToolCallStatus.WAITING_APPROVAL,
    )
    _make_tool_call(
        harness, tool_call_id="call-planned", tool_name="host_read",
        status=ToolCallStatus.PLANNED,
    )

    _, service, recovery = build_recovery(harness, registry)
    await recovery.shutdown()

    store = harness.investigations
    assert store.get_tool_call("call-danger").status is ToolCallStatus.UNCERTAIN
    assert store.get_tool_call("call-waiting").status is ToolCallStatus.CANCELLED
    assert store.get_tool_call("call-planned").status is ToolCallStatus.CANCELLED
    remaining = store.list_tool_calls(agent_run_id="run-1")
    assert all(tool.status in ToolCallStatus for tool in remaining)
    assert all(not _is_in_flight(tool.status) for tool in remaining)


def _is_in_flight(status: ToolCallStatus) -> bool:
    return status in {
        ToolCallStatus.PLANNED,
        ToolCallStatus.WAITING_APPROVAL,
        ToolCallStatus.RUNNING,
    }


@pytest.mark.asyncio
async def test_shutdown_drains_active_loop_and_marks_interrupted_tool_uncertain(
    tmp_path: Any,
) -> None:
    """An active loop is drained within grace; a hung dangerous tool is swept.

    The tool call is stamped RUNNING by the orchestrator before the executor
    runs (C1), so when shutdown interrupts the hung execution the sweep sees a
    RUNNING dangerous call and marks it UNCERTAIN -- never left dangling and
    never replayed.
    """
    harness = build_harness(
        tmp_path,
        transport_factory=HarnessTransportFactory(hang_shell=True),
    )
    registry = FakeProviderRegistry()
    _make_investigation(harness)
    _make_run(harness)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    tool_request(
                        "shell_exec",
                        tool_call_id="call-1",
                        service_name=SERVICE,
                        command="docker ps",
                        timeout_seconds=30,
                    ),
                )
            )
        ],
    )
    orchestrator, service, recovery = build_recovery(
        harness, registry, shutdown_grace_seconds=0.5
    )

    loop_task = asyncio.create_task(orchestrator.run("run-1"))
    await asyncio.sleep(0.2)  # let the loop begin executing the hung tool
    assert not loop_task.done()
    # C1: the executing call is persisted RUNNING, not PLANNED.
    assert (
        harness.investigations.get_tool_call_by_provider_id("run-1", "call-1").status
        is ToolCallStatus.RUNNING
    )

    cancelled = await recovery.shutdown()

    assert cancelled == 1
    assert loop_task.done()
    assert service.get_run("run-1").status is AgentRunStatus.CANCELLED
    assert service.get_investigation("inv-1").status is InvestigationStatus.CANCELLED
    # The interrupted shell call could not be confirmed -> UNCERTAIN, never left
    # as a dangling RUNNING row.
    tool_call = harness.investigations.get_tool_call_by_provider_id("run-1", "call-1")
    assert tool_call.status is ToolCallStatus.UNCERTAIN
    assert "never replayed" in tool_call.error_redacted


@pytest.mark.asyncio
async def test_shutdown_is_idempotent_and_cancels_created_investigations(
    tmp_path: Any,
) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.CREATED)
    _make_run(harness, status=AgentRunStatus.CREATED)

    _, service, recovery = build_recovery(harness, registry)
    first = await recovery.shutdown()
    second = await recovery.shutdown()

    assert first == 1
    assert second == 0
    assert service.get_investigation("inv-1").status is InvestigationStatus.CANCELLED
    assert service.get_run("run-1").status is AgentRunStatus.CANCELLED


# ---------------------------------------------------------------------------
# C1/C2 regressions: RUNNING persistence, cancel-vs-approval, cancel sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_execution_crash_marks_dangerous_in_flight_uncertain(
    tmp_path: Any,
) -> None:
    """A crash under the real execution model leaves RUNNING -> UNCERTAIN.

    The orchestrator persists the tool call as RUNNING before the executor
    runs (C1), so a crash mid-execution leaves a RUNNING dangerous call that
    startup recovery parks UNCERTAIN -- it is never replayed on resume.
    """
    harness = build_harness(
        tmp_path,
        transport_factory=HarnessTransportFactory(hang_shell=True),
    )
    registry = FakeProviderRegistry()
    _make_investigation(harness)
    _make_run(harness)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    tool_request(
                        "shell_exec",
                        tool_call_id="call-1",
                        service_name=SERVICE,
                        command="docker ps",
                        timeout_seconds=30,
                    ),
                )
            )
        ],
    )
    orchestrator, service, recovery = build_recovery(harness, registry)

    # Crash mid-execution: kill the loop without any orderly shutdown.
    loop_task = asyncio.create_task(orchestrator.run("run-1"))
    await asyncio.sleep(0.2)
    loop_task.cancel()
    await asyncio.gather(loop_task, return_exceptions=True)
    # C1: the dangerous call is persisted RUNNING after the crash.
    assert (
        harness.investigations.get_tool_call_by_provider_id("run-1", "call-1").status
        is ToolCallStatus.RUNNING
    )

    # Restart recovery classifies it: parked UNCERTAIN, never replayed.
    summary = await recovery.startup()
    assert summary.dangerous_parked == 1
    assert service.get_run("run-1").status is AgentRunStatus.PAUSED_UNCERTAIN_STATE
    assert (
        service.get_investigation("inv-1").status
        is InvestigationStatus.PAUSED_UNCERTAIN_STATE
    )
    tool_call = harness.investigations.get_tool_call_by_provider_id("run-1", "call-1")
    assert tool_call.status is ToolCallStatus.UNCERTAIN
    assert "never replayed" in tool_call.error_redacted


@pytest.mark.asyncio
async def test_approval_after_cancel_does_not_execute_tool(tmp_path: Any) -> None:
    """An approval landing after the run was cancelled never re-executes."""
    harness = build_harness(
        tmp_path,
        transport_factory=HarnessTransportFactory(
            shell_output=b"restarted", shell_status=0
        ),
    )
    registry = FakeProviderRegistry()
    _make_investigation(harness)
    _make_run(harness)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    tool_request(
                        "shell_exec",
                        tool_call_id="call-1",
                        service_name=SERVICE,
                        command="systemctl restart mysql",
                    ),
                )
            )
        ],
    )
    _, service, _ = build_recovery(harness, registry)

    parked = await service.resume_run("run-1")
    assert parked.status is AgentRunStatus.WAITING_APPROVAL
    approval = harness.approvals.list()[0]
    await harness.approvals.approve(approval.approval_id)

    # The operator cancels the investigation after the approval was granted.
    await service.cancel("inv-1")
    assert service.get_run("run-1").status is AgentRunStatus.CANCEL_REQUESTED

    outcome = await service.handle_approval_decision(approval.approval_id)

    assert outcome.action == "cancelled"
    assert outcome.applied is False
    # No transport contact: the dangerous tool was never re-executed.
    assert harness.factory.transports == []
    assert (
        harness.investigations.get_tool_call_by_provider_id("run-1", "call-1").status
        is ToolCallStatus.CANCELLED
    )


@pytest.mark.asyncio
async def test_approval_on_waiting_approval_run_under_cancelled_investigation_skipped(
    tmp_path: Any,
) -> None:
    """A decision on a WAITING_APPROVAL run under a CANCELLED investigation is
    skipped: ``_run_cancel_pending`` checks the owning investigation, so a
    crash-mid-cancel orphan can never re-execute a dangerous tool."""
    harness = build_harness(
        tmp_path,
        transport_factory=HarnessTransportFactory(
            shell_output=b"restarted", shell_status=0
        ),
    )
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.CANCELLED)
    _make_run(harness, status=AgentRunStatus.WAITING_APPROVAL)
    approval = await harness.approvals.request({"kind": "shell", "target_id": TARGET_ID})
    await harness.approvals.approve(approval.approval_id)
    call = ToolCall(
        tool_call_id="call-1",
        agent_run_id="run-1",
        tool_name="shell_exec",
        status=ToolCallStatus.PLANNED,
        idempotency_key="call-1",
        planned_at=NOW,
    )
    harness.investigations.create_tool_call(call)
    harness.investigations.transition_tool_call_status(
        "call-1",
        ToolCallStatus.WAITING_APPROVAL,
        now=NOW,
        approval_id=approval.approval_id,
    )

    _, service, _ = build_recovery(harness, registry)
    outcome = await service.handle_approval_decision(approval.approval_id)

    assert outcome.action == "cancelled"
    assert outcome.applied is False
    assert harness.factory.transports == []
    assert (
        harness.investigations.get_tool_call_by_provider_id("run-1", "call-1").status
        is ToolCallStatus.CANCELLED
    )


@pytest.mark.asyncio
async def test_startup_cancelled_investigation_sweeps_waiting_approval_run(
    tmp_path: Any,
) -> None:
    """A crash-mid-cancel leaves a WAITING_APPROVAL run under a cancelled
    investigation; startup sweeps the run and never re-executes the approved
    tool (C2 residual fix)."""
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.CANCEL_REQUESTED)
    _make_run(harness, status=AgentRunStatus.WAITING_APPROVAL)
    approval = await harness.approvals.request({"kind": "shell", "target_id": TARGET_ID})
    await harness.approvals.approve(approval.approval_id)
    call = ToolCall(
        tool_call_id="call-1",
        agent_run_id="run-1",
        tool_name="shell_exec",
        status=ToolCallStatus.PLANNED,
        idempotency_key="call-1",
        planned_at=NOW,
    )
    harness.investigations.create_tool_call(call)
    harness.investigations.transition_tool_call_status(
        "call-1",
        ToolCallStatus.WAITING_APPROVAL,
        now=NOW,
        approval_id=approval.approval_id,
    )

    _, service, recovery = build_recovery(harness, registry)
    summary = await recovery.startup()

    assert summary.cancel_finalised == 1
    assert summary.reconciled_approvals == 0
    assert service.get_investigation("inv-1").status is InvestigationStatus.CANCELLED
    assert service.get_run("run-1").status is AgentRunStatus.CANCELLED
    assert (
        harness.investigations.get_tool_call_by_provider_id("run-1", "call-1").status
        is ToolCallStatus.CANCELLED
    )
    assert harness.factory.transports == []


@pytest.mark.asyncio
async def test_startup_does_not_reexecute_approved_tool_on_cancelled_run(
    tmp_path: Any,
) -> None:
    """Startup finalises cancels before reconciling, so nothing re-executes."""
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.CANCEL_REQUESTED)
    _make_run(harness, status=AgentRunStatus.CANCEL_REQUESTED)
    approval = await harness.approvals.request({"kind": "shell", "target_id": TARGET_ID})
    await harness.approvals.approve(approval.approval_id)
    # A WAITING_APPROVAL tool call linked to the approved approval, as a crash
    # would leave it.
    call = ToolCall(
        tool_call_id="call-1",
        agent_run_id="run-1",
        tool_name="shell_exec",
        status=ToolCallStatus.PLANNED,
        idempotency_key="call-1",
        planned_at=NOW,
    )
    harness.investigations.create_tool_call(call)
    harness.investigations.transition_tool_call_status(
        "call-1",
        ToolCallStatus.WAITING_APPROVAL,
        now=NOW,
        approval_id=approval.approval_id,
    )

    _, service, recovery = build_recovery(harness, registry)
    summary = await recovery.startup()

    assert summary.cancel_finalised == 1
    assert summary.reconciled_approvals == 0
    assert service.get_run("run-1").status is AgentRunStatus.CANCELLED
    assert service.get_investigation("inv-1").status is InvestigationStatus.CANCELLED
    # The tool was swept to CANCELLED (M1), never re-executed.
    assert (
        harness.investigations.get_tool_call_by_provider_id("run-1", "call-1").status
        is ToolCallStatus.CANCELLED
    )
    assert harness.factory.transports == []


@pytest.mark.asyncio
async def test_startup_finalises_cancel_sweeps_in_flight_tool_calls(
    tmp_path: Any,
) -> None:
    """A crash-mid-cancel run's in-flight calls are swept before finalisation."""
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.CANCEL_REQUESTED)
    _make_run(harness, status=AgentRunStatus.CANCEL_REQUESTED)
    _make_tool_call(harness, tool_call_id="call-danger", tool_name="docker_action")
    _make_tool_call(
        harness, tool_call_id="call-waiting", tool_name="shell_exec",
        status=ToolCallStatus.WAITING_APPROVAL,
    )
    _make_tool_call(
        harness, tool_call_id="call-planned", tool_name="host_read",
        status=ToolCallStatus.PLANNED,
    )

    _, service, recovery = build_recovery(harness, registry)
    summary = await recovery.startup()

    assert summary.cancel_finalised == 1
    store = harness.investigations
    assert store.get_tool_call("call-danger").status is ToolCallStatus.UNCERTAIN
    assert store.get_tool_call("call-waiting").status is ToolCallStatus.CANCELLED
    assert store.get_tool_call("call-planned").status is ToolCallStatus.CANCELLED
    assert service.get_run("run-1").status is AgentRunStatus.CANCELLED
    # UNCERTAIN_STATE evidence was recorded for the dangerous in-flight call.
    stored = harness.evidence_store.query(agent_run_id="run-1")
    assert any(item.evidence_kind is EvidenceKind.UNCERTAIN_STATE for item in stored)


# ---------------------------------------------------------------------------
# Wiring: bounded active-investigation cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_active_investigations_is_bounded(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _, service, _ = build_recovery(
        harness, registry, max_active_investigations=1
    )
    first = service.create_investigation(
        project_id=PROJECT_ID, target_id=TARGET_ID, service=SERVICE, symptom="one"
    )
    assert first.status is InvestigationStatus.CREATED
    with pytest.raises(TooManyActiveInvestigations):
        service.create_investigation(
            project_id=PROJECT_ID, target_id=TARGET_ID, service=SERVICE, symptom="two"
        )
