"""Behavioral deterministic scenarios over the real bounded runtime."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from incidentlens_control_plane.investigation.compactor import ContextCompactor, CompactionRequest
from incidentlens_control_plane.investigation.fake_provider import (
    DelegateChildStep, FakeProvider, FakeProviderRegistry, RequestToolsStep,
    StopStep,
)
from incidentlens_control_plane.investigation.provider import (
    ChildDelegationRequest, Conclusion, PromptTooLongError, StopSignal, ToolRequest,
)
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus, InvestigationStatus, ToolCallStatus,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget, AgentScope, CompactBoundary, SessionMemory, StopReason,
)
from incidentlens_control_plane.logs.types import LogScope

from .support import (
    NOW, CONTAINER, CONTAINER_ROOT, PROJECT_ID, SERVICE, TARGET_ID,
    build_harness, make_orchestrator, make_scope, seed_evidence, seed_run,
)
from .types import HarnessTrace


def tool_request(tool_name: str, tool_call_id: str, **arguments: Any) -> ToolRequest:
    return ToolRequest(tool_call_id=tool_call_id, tool_name=tool_name, arguments=arguments)


def completed_step(evidence_id: str) -> StopStep:
    return StopStep(
        stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="diagnosis complete"),
        conclusion=Conclusion(summary="root cause identified", evidence_ids=(evidence_id,)),
    )


def _receipt_if_present(harness: Any, child_id: str) -> Any | None:
    try:
        return harness.investigations.get_child_report_receipt(child_id)
    except KeyError:
        return None


def _trace(harness: Any, scenario: str) -> HarnessTrace:
    run = harness.investigations.get_agent_run("run-1")
    child_runs = harness.investigations.list_agent_runs(parent_run_id=run.agent_run_id)
    child_ids = tuple(child.agent_run_id for child in child_runs)
    receipts = tuple(
        receipt
        for child_id in child_ids
        if (receipt := _receipt_if_present(harness, child_id)) is not None
    )
    forms = tuple(
        "delegate_child_tool"
        if any(
            call.tool_name == "delegate_child"
            and call.arguments.get("child_run_id") == child_id
            for call in harness.investigations.list_tool_calls(agent_run_id=run.agent_run_id)
        )
        else "typed_delegation"
        for child_id in child_ids
    )
    return HarnessTrace(
        scenario=scenario,
        investigation=harness.investigations.get_investigation(run.investigation_id),
        run=run,
        rounds=harness.investigations.list_rounds(run.agent_run_id),
        tool_calls=harness.investigations.list_tool_calls(agent_run_id=run.agent_run_id),
        transcript=harness.investigations.list_transcript_messages(run.agent_run_id),
        compact_boundaries=harness.investigations.list_compact_boundaries(run.agent_run_id),
        evidence=run.evidence,
        conclusions=harness.investigations.list_conclusions(agent_run_id=run.agent_run_id),
        child_receipts=receipts,
        hook_events=harness.events.list_after(0, 1000),
        mutation_tool_call_ids=tuple(
            call.tool_call_id for call in harness.investigations.list_tool_calls(agent_run_id=run.agent_run_id)
            if call.tool_name in {"file_write", "file_edit", "docker_action", "shell_exec"}
        ),
        expected_child_run_ids=child_ids,
        delegation_forms=forms,
    )


async def run_grounded_diagnosis() -> HarnessTrace:
    with tempfile.TemporaryDirectory(prefix="incidentlens-grounded-") as directory:
        harness = build_harness(Path(directory)); seed_run(harness, budget=AgentBudget(max_rounds=8, max_tool_calls=8))
        evidence_id = seed_evidence(harness)
        registry = FakeProviderRegistry(); registry.set_script("run-1", [completed_step(evidence_id)])
        await make_orchestrator(harness, FakeProvider(registry)).run("run-1")
        trace = _trace(harness, "grounded_diagnosis")
        assert trace.run.status is AgentRunStatus.COMPLETED and trace.conclusions
        return trace


class _DeterministicCompactor(ContextCompactor):
    def __init__(self, store: Any) -> None:
        self.store = store
        self.requests: list[CompactionRequest] = []

    async def compact(self, request: CompactionRequest) -> SessionMemory:
        self.requests.append(request)
        return SessionMemory(
            memory_id=f"memory-{len(self.requests)}", agent_run_id=request.agent_run_id,
            investigation_id=request.investigation_id, revision=1,
            through_round=request.through_round,
            through_transcript_sequence=max(request.through_sequence, 1),
            objective="recover bounded context", confirmed_facts=("prior turn persisted",),
            evidence_ids=request.allowed_evidence_ids[:1], created_at=NOW,
        )


async def run_context_overflow_recovery() -> HarnessTrace:
    with tempfile.TemporaryDirectory(prefix="incidentlens-overflow-") as directory:
        harness = build_harness(Path(directory)); seed_run(harness, budget=AgentBudget(max_rounds=4))
        evidence_id = seed_evidence(harness)
        registry = FakeProviderRegistry(); registry.set_script("run-1", [RequestToolsStep(tool_requests=(tool_request("registry_info", "overflow-seed"),)), PromptTooLongError(), completed_step(evidence_id)])
        compactor = _DeterministicCompactor(harness.investigations)
        from incidentlens_control_plane.investigation.context import AgentContextManager, ContextBudgetPolicy
        context = AgentContextManager(
            harness.investigations,
            policy=ContextBudgetPolicy(context_window=8_000, max_output_tokens=1_000, reserve_tokens=0,
                                       max_message_groups=1, reactive_keep_recent_groups=1),
            now=lambda: NOW, compactor=compactor,
        )
        await make_orchestrator(harness, FakeProvider(registry), context_manager=context).run("run-1")
        trace = _trace(harness, "context_overflow_recovery")
        assert len(compactor.requests) == 1 and trace.compact_boundaries
        assert trace.run.status is AgentRunStatus.COMPLETED
        return trace


async def run_scope_violation() -> HarnessTrace:
    with tempfile.TemporaryDirectory(prefix="incidentlens-scope-") as directory:
        harness = build_harness(Path(directory)); seed_run(harness, scope=make_scope(container=True), budget=AgentBudget(max_rounds=8, max_tool_calls=8))
        registry = FakeProviderRegistry(); registry.set_script("run-1", [RequestToolsStep(tool_requests=(tool_request("host_read", "scope-call", service_name=SERVICE, path="/opt/payments/secret"),))])
        await make_orchestrator(harness, FakeProvider(registry)).run("run-1")
        trace = _trace(harness, "scope_violation")
        calls = trace.tool_calls
        assert calls and calls[0].status is ToolCallStatus.FAILED
        assert not harness.transport_factory.transports
        return trace


async def run_approval_pause_resume() -> HarnessTrace:
    with tempfile.TemporaryDirectory(prefix="incidentlens-approval-") as directory:
        harness = build_harness(Path(directory)); seed_run(harness, budget=AgentBudget(max_rounds=8, max_tool_calls=8))
        registry = FakeProviderRegistry(); registry.set_script("run-1", [RequestToolsStep(tool_requests=(tool_request("docker_action", "mutation-call", service_name=SERVICE, action="restart", container=CONTAINER, reason="diagnose"),)), completed_step("missing")])
        orchestrator = make_orchestrator(harness, FakeProvider(registry))
        parked = await orchestrator.run("run-1")
        call = harness.investigations.list_tool_calls(agent_run_id="run-1")[0]
        assert parked.status is AgentRunStatus.WAITING_APPROVAL and call.approval_id
        await harness.approvals.approve(call.approval_id)
        from incidentlens_control_plane.investigation.service import InvestigationService
        service = InvestigationService(store=harness.investigations, orchestrator=orchestrator, now=lambda: NOW, approvals=harness.approvals, executor=harness.executor)
        await service.handle_approval_decision(call.approval_id, now=NOW)
        trace = _trace(harness, "approval_pause_resume")
        assert trace.tool_calls[0].status is ToolCallStatus.SUCCEEDED
        return trace


async def _delegation_trace(name: str) -> HarnessTrace:
    with tempfile.TemporaryDirectory(prefix="incidentlens-delegate-") as directory:
        harness = build_harness(Path(directory)); seed_run(harness, budget=AgentBudget(max_rounds=8, max_tool_calls=8))
        scope = make_scope()
        registry = FakeProviderRegistry()
        registry.set_script("run-1", [
            DelegateChildStep(delegation=ChildDelegationRequest(child_run_id="child-typed", task_prompt="inspect", scope=scope)),
            RequestToolsStep(tool_requests=(tool_request("registry_info", "parent-evidence"),)),
            StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="parent")),
        ])
        registry.set_script("child-typed", [RequestToolsStep(tool_requests=(tool_request("registry_info", "child-evidence"),)), StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="child"))])
        await make_orchestrator(harness, FakeProvider(registry)).run("run-1")
        trace = _trace(harness, name)
        assert trace.child_receipts and trace.child_receipts[0].delivered_at is not None
        return trace


async def run_delegation_equivalence() -> HarnessTrace:
    typed = await _delegation_trace("delegation_equivalence")
    # Exercise the alternate tool-request form through the same orchestrator boundary.
    with tempfile.TemporaryDirectory(prefix="incidentlens-delegate-tool-") as directory:
        harness = build_harness(Path(directory)); seed_run(harness, budget=AgentBudget(max_rounds=8, max_tool_calls=8))
        scope = make_scope()
        registry = FakeProviderRegistry()
        registry.set_script("run-1", [
            RequestToolsStep(tool_requests=(tool_request("delegate_child", "tool-delegate", child_run_id="child-tool", task_prompt="inspect", scope={"project_id": PROJECT_ID, "target_id": TARGET_ID, "scope": "host", "allowed_host_paths": ["/opt/payments"]}),)),
            RequestToolsStep(tool_requests=(tool_request("registry_info", "parent-evidence"),)),
            StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="parent")),
        ])
        registry.set_script("child-tool", [RequestToolsStep(tool_requests=(tool_request("registry_info", "child-evidence"),)), StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="child"))])
        await make_orchestrator(harness, FakeProvider(registry)).run("run-1")
        tool_trace = _trace(harness, "delegation-equivalence-tool")
        assert tool_trace.child_receipts and tool_trace.child_receipts[0].delivered_at is not None
        assert tool_trace.delegation_forms == ("delegate_child_tool",)
    return typed.model_copy(update={
        "delegation_forms": ("typed_delegation", "delegate_child_tool"),
        "expected_child_run_ids": typed.expected_child_run_ids + tool_trace.expected_child_run_ids,
        "child_receipts": typed.child_receipts + tool_trace.child_receipts,
    })


async def run_child_restart_delivery() -> HarnessTrace:
    with tempfile.TemporaryDirectory(prefix="incidentlens-restart-") as directory:
        path = Path(directory); harness = build_harness(path); seed_run(harness, budget=AgentBudget(max_rounds=8, max_tool_calls=8))
        registry = FakeProviderRegistry(); registry.set_script("child-1", [RequestToolsStep(tool_requests=(tool_request("registry_info", "child-evidence"),)), StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="child"))])
        orchestrator = make_orchestrator(harness, FakeProvider(registry))
        parent = harness.investigations.get_agent_run("run-1"); investigation = harness.investigations.get_investigation("inv-1")
        pending: list[tuple[str, Any]] = []
        await orchestrator._delegate_child(parent, investigation, ChildDelegationRequest(child_run_id="child-1", task_prompt="inspect", scope=make_scope()), pending, NOW)
        await pending[0][1]
        registry.set_script("run-1", [StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="parent"))])
        # Leave the receipt pending: no parent loop or provider turn may consume it.
        pending_receipt = harness.investigations.list_undelivered_child_report_receipts("run-1")
        assert len(pending_receipt) == 1 and pending_receipt[0].delivered_at is None
        restarted = build_harness(path, transport_factory=harness.transport_factory)
        from incidentlens_control_plane.investigation.service import InvestigationService
        recovery_orchestrator = make_orchestrator(restarted, FakeProvider(registry))
        recovery_service = InvestigationService(store=restarted.investigations, orchestrator=recovery_orchestrator, now=lambda: NOW, approvals=restarted.approvals, executor=restarted.executor)
        from incidentlens_control_plane.investigation.recovery import RecoveryService
        summary = await RecoveryService(store=restarted.investigations, investigations=recovery_service, orchestrator=recovery_orchestrator, evidence=restarted.evidence, approvals=restarted.approvals, now=lambda: NOW, events=restarted.events, broker=restarted.broker).startup()
        assert summary.reconciled_child_receipts == 1
        post_recovery = restarted.investigations.get_child_report_receipt("child-1")
        assert post_recovery.delivered_at is not None
        # A second recovery is idempotent and cannot duplicate the notification.
        second = await RecoveryService(store=restarted.investigations, investigations=recovery_service, orchestrator=recovery_orchestrator, evidence=restarted.evidence, approvals=restarted.approvals, now=lambda: NOW, events=restarted.events, broker=restarted.broker).startup()
        assert second.reconciled_child_receipts == 0
        trace = _trace(restarted, "child_restart_delivery")
        assert len([m for m in trace.transcript if any("Child report child-1" in getattr(b, "text", "") for b in m.blocks)]) == 1
        return trace


SCENARIOS = (
    ("grounded_diagnosis", run_grounded_diagnosis),
    ("context_overflow_recovery", run_context_overflow_recovery),
    ("scope_violation", run_scope_violation),
    ("approval_pause_resume", run_approval_pause_resume),
    ("delegation_equivalence", run_delegation_equivalence),
    ("child_restart_delivery", run_child_restart_delivery),
)
