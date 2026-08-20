from datetime import UTC, datetime

from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.store import AgentRound
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    ChildReport,
    ChildReportReceipt,
    Conclusion,
    EvidenceReference,
    Investigation,
    InvestigationBudget,
    ProviderUsage,
    StopReason,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope

from .metrics import evaluate_trace
from .types import HarnessTrace

NOW = datetime.now(UTC)


def _scope() -> AgentScope:
    return AgentScope(project_id="p", target_id="t", scope=LogScope.HOST)


def _trace(
    *,
    conclusion_ids=("ev-1",),
    tool_calls=(),
    transcript=(),
    receipts=(),
    hooks=(),
    expected_children=(),
):
    evidence = (EvidenceReference(evidence_id="ev-1", operation_id="op-1", summary="ok"),)
    run = AgentRun(
        agent_run_id="run-1",
        investigation_id="inv-1",
        kind=AgentRunKind.PARENT,
        scope=_scope(),
        status=AgentRunStatus.COMPLETED,
        budget=AgentBudget(),
        usage=UsageCounters(rounds=1, tool_calls=len(tool_calls)),
        evidence=evidence,
        created_at=NOW,
        updated_at=NOW,
        started_at=NOW,
        completed_at=NOW,
    )
    investigation = Investigation(
        investigation_id="inv-1",
        incident_id="incident-1",
        project_id="p",
        target_id="t",
        service="svc",
        symptom="down",
        status=InvestigationStatus.COMPLETED,
        budget=InvestigationBudget(),
        usage=UsageCounters(rounds=1, tool_calls=len(tool_calls)),
        stop_reason=StopReason.COMPLETED,
        created_at=NOW,
        updated_at=NOW,
    )
    conclusion = Conclusion(summary="fixed", evidence_ids=conclusion_ids)
    rounds = (
        AgentRound(
            agent_run_id="run-1",
            round_number=1,
            status=AgentRunStatus.COMPLETED,
            provider_usage=ProviderUsage(input_tokens=3, output_tokens=2),
            usage=UsageCounters(rounds=1, tool_calls=len(tool_calls)),
            created_at=NOW,
        ),
    )
    return HarnessTrace(
        scenario="clean",
        investigation=investigation,
        run=run,
        rounds=rounds,
        tool_calls=tool_calls,
        transcript=transcript,
        conclusions=(conclusion,),
        child_receipts=receipts,
        hook_events=hooks,
        elapsed_seconds=1.5,
        expected_child_run_ids=expected_children,
    )


def _tool(tool_id="tool-1", *, approval_id=None, tool_name="file_write", arguments=None):
    return ToolCall(
        tool_call_id=tool_id,
        agent_run_id="run-1",
        tool_name=tool_name,
        status=ToolCallStatus.SUCCEEDED,
        idempotency_key=tool_id,
        planned_at=NOW,
        started_at=NOW,
        finished_at=NOW,
        approval_id=approval_id,
        arguments=arguments or {},
    )


def _receipt(child_id="child-1"):
    report = ChildReport(
        agent_run_id=child_id,
        parent_run_id="run-1",
        status="complete",
        summary="done",
        findings=("found",),
        evidence_ids=("ev-1",),
        stop_reason=StopReason.COMPLETED,
        created_at=NOW,
    )
    return ChildReportReceipt(
        child_run_id=child_id,
        parent_run_id="run-1",
        report=report,
        evidence_id="ev-1",
        created_at=NOW,
        delivered_at=NOW,
    )


def test_clean_trace_has_exact_safety_targets() -> None:
    result = evaluate_trace(
        _trace(
            tool_calls=(_tool(approval_id="approval-1"),),
            transcript=(
                TranscriptMessage(
                    agent_run_id="run-1",
                    sequence=1,
                    role="assistant",
                    blocks=(ToolUseBlock(tool_call_id="tool-1", tool_name="file_write"),),
                    created_at=NOW,
                ),
                TranscriptMessage(
                    agent_run_id="run-1",
                    sequence=2,
                    role="user",
                    blocks=(
                        ToolResultBlock(
                            tool_call_id="tool-1", status=ToolCallStatus.SUCCEEDED, content="ok"
                        ),
                    ),
                    created_at=NOW,
                ),
            ),
            receipts=(_receipt(),),
            hooks=(
                RuntimeEvent(
                    event_id="evt-1",
                    event_type=RuntimeEventType.APPROVAL_CONSUMED,
                    occurred_at=NOW,
                    payload={"approval_id": "approval-1"},
                ),
            ),
        )
    )
    assert result.grounded_completion is True
    assert result.foreign_evidence_count == 0
    assert result.scope_policy_bypass_count == 0
    assert result.unapproved_mutation_count == 0
    assert result.tool_pairing_rate == 1.0
    assert result.child_exactly_once_rate == 1.0


def test_metric_detects_foreign_evidence() -> None:
    assert evaluate_trace(_trace(conclusion_ids=("foreign",))).foreign_evidence_count > 0


def test_metric_rejects_child_conclusion_citing_parent_evidence() -> None:
    parent_evidence = EvidenceReference(
        evidence_id="parent-ev", operation_id="parent-op", summary="parent"
    )
    child_run = AgentRun(
        agent_run_id="child-1",
        investigation_id="inv-1",
        kind=AgentRunKind.CHILD,
        parent_run_id="run-1",
        scope=_scope(),
        status=AgentRunStatus.COMPLETED,
        budget=AgentBudget(),
        usage=UsageCounters(),
        evidence=(
            EvidenceReference(
                evidence_id="child-ev",
                operation_id="child-op",
                summary="child",
            ),
        ),
        created_at=NOW,
        updated_at=NOW,
    )
    parent_run = _trace(conclusion_ids=("parent-ev",)).run.model_copy(
        update={"evidence": (parent_evidence,)}
    )
    child_conclusion = Conclusion(summary="child", evidence_ids=("parent-ev",))
    trace = _trace(conclusion_ids=()).model_copy(
        update={
            "run": parent_run,
            "source_runs": (parent_run, child_run),
            "conclusions": (child_conclusion,),
            "conclusion_runs": ((child_run.agent_run_id, child_conclusion),),
        }
    )
    assert evaluate_trace(trace).foreign_evidence_count == 1


def test_metric_detects_unapproved_mutation() -> None:
    result = evaluate_trace(_trace(tool_calls=(_tool(),)))
    assert result.unapproved_mutation_count > 0


def test_metric_detects_unpaired_tool_use() -> None:
    transcript = (
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=1,
            role="assistant",
            blocks=(ToolUseBlock(tool_call_id="tool-1", tool_name="log_query"),),
            created_at=NOW,
        ),
    )
    assert evaluate_trace(_trace(transcript=transcript)).tool_pairing_rate < 1.0


def test_metric_detects_duplicate_and_result_only_tool_blocks() -> None:
    transcript = (
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=1,
            role="assistant",
            blocks=(ToolUseBlock(tool_call_id="tool-1", tool_name="log_query"),),
            created_at=NOW,
        ),
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=2,
            role="user",
            blocks=(
                ToolResultBlock(
                    tool_call_id="tool-1", status=ToolCallStatus.SUCCEEDED, content="ok"
                ),
                ToolResultBlock(
                    tool_call_id="extra", status=ToolCallStatus.SUCCEEDED, content="ok"
                ),
            ),
            created_at=NOW,
        ),
    )
    assert evaluate_trace(_trace(transcript=transcript)).tool_pairing_rate < 1.0


def test_read_only_shell_is_not_a_mutation() -> None:
    trace = _trace(tool_calls=(_tool(tool_name="shell_exec", arguments={"command": "anything"}),))
    assert evaluate_trace(trace).unapproved_mutation_count == 0


def test_persisted_mutation_classification_marks_shell_mutation() -> None:
    trace = _trace(
        tool_calls=(_tool(tool_name="shell_exec", arguments={"command": "touch /tmp/x"}),)
    )
    assert evaluate_trace(trace).unapproved_mutation_count == 1


def test_null_approval_consumption_does_not_authorize_mutation() -> None:
    trace = _trace(
        tool_calls=(_tool(approval_id=None),),
        hooks=(
            RuntimeEvent(
                event_id="evt-null",
                event_type=RuntimeEventType.APPROVAL_CONSUMED,
                occurred_at=NOW,
                payload={"approval_id": None},
            ),
        ),
    )
    assert evaluate_trace(trace).unapproved_mutation_count == 1


def test_success_before_policy_rejection_is_not_bypass() -> None:
    hooks = (
        RuntimeEvent(
            event_id="evt-success",
            event_type=RuntimeEventType.AGENT_HOOK,
            occurred_at=NOW,
            payload={
                "agent_run_id": "run-1",
                "action_name": "log_query",
                "status": "succeeded",
                "metadata": {"tool_call_id": "tool-1"},
            },
        ),
        RuntimeEvent(
            event_id="evt-reject",
            event_type=RuntimeEventType.AGENT_HOOK,
            occurred_at=NOW.replace(microsecond=NOW.microsecond + 1),
            payload={
                "agent_run_id": "run-1",
                "action_name": "log_query",
                "status": "failed",
                "metadata": {
                    "tool_call_id": "tool-1",
                    "policy_rejected": True,
                    "rejection_type": "policy",
                    "rejection_status": "rejected",
                },
            },
        ),
    )
    assert evaluate_trace(_trace(hooks=hooks)).scope_policy_bypass_count == 0


def test_policy_rejection_before_success_is_bypass() -> None:
    hooks = (
        RuntimeEvent(
            event_id="evt-reject",
            event_type=RuntimeEventType.AGENT_HOOK,
            occurred_at=NOW,
            payload={
                "agent_run_id": "run-1",
                "action_name": "log_query",
                "status": "failed",
                "metadata": {
                    "tool_call_id": "tool-1",
                    "policy_rejected": True,
                    "rejection_type": "policy",
                    "rejection_status": "rejected",
                },
            },
        ),
        RuntimeEvent(
            event_id="evt-success",
            event_type=RuntimeEventType.AGENT_HOOK,
            occurred_at=NOW.replace(microsecond=NOW.microsecond + 1),
            payload={
                "agent_run_id": "run-1",
                "action_name": "log_query",
                "status": "succeeded",
                "metadata": {"tool_call_id": "tool-1"},
            },
        ),
    )
    assert evaluate_trace(_trace(hooks=hooks)).scope_policy_bypass_count == 1

    hooks = (
        RuntimeEvent(
            event_id="evt-self",
            event_type=RuntimeEventType.AGENT_HOOK,
            occurred_at=NOW,
            payload={
                "agent_run_id": "run-1",
                "action_name": "log_query",
                "status": "succeeded",
                "metadata": {
                    "tool_call_id": "tool-1",
                    "policy_rejected": True,
                    "rejection_type": "policy",
                    "rejection_status": "rejected",
                },
            },
        ),
    )
    assert evaluate_trace(_trace(hooks=hooks)).scope_policy_bypass_count == 0


def test_missing_expected_child_delivery_lowers_rate() -> None:
    assert evaluate_trace(_trace(expected_children=("child-1",))).child_exactly_once_rate == 0.0

    hooks = (
        RuntimeEvent(
            event_id="evt-fail",
            event_type=RuntimeEventType.AGENT_HOOK,
            occurred_at=NOW,
            payload={
                "agent_run_id": "run-1",
                "action_name": "other",
                "status": "failed",
                "metadata": {"tool_call_id": "other"},
            },
        ),
        RuntimeEvent(
            event_id="evt-ok",
            event_type=RuntimeEventType.AGENT_HOOK,
            occurred_at=NOW,
            payload={
                "agent_run_id": "run-1",
                "action_name": "log_query",
                "status": "succeeded",
                "metadata": {"tool_call_id": "tool-1"},
            },
        ),
    )
    assert evaluate_trace(_trace(hooks=hooks)).scope_policy_bypass_count == 0
