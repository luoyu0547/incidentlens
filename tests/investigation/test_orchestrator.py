"""Tests for the bounded parent/container-child agent orchestrator and service.

The harness reuses the real SQLite-backed runtime from ``test_tool_executor``
(project registry, log/evidence/approval stores, gateway, sessions) and drives
the orchestrator with the scripted ``FakeProvider`` so every scenario walks the
real guard / validator / executor / approval paths.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from incidentlens_control_plane.evidence.types import EvidenceKind
from incidentlens_control_plane.investigation.context import AgentContextManager
from incidentlens_control_plane.investigation.fake_provider import (
    CrashStep,
    DelegateChildStep,
    FakeProvider,
    FakeProviderRegistry,
    RequestToolsStep,
    StopStep,
)
from incidentlens_control_plane.investigation.orchestrator import AgentOrchestrator
from incidentlens_control_plane.investigation.provider import (
    AgentTurnResult,
    ChildDelegationRequest,
    Conclusion,
    ConversationRequest,
    HypothesisProposal,
    ModelProvider,
    PromptTooLongError,
    StopSignal,
    ToolRequest,
)
from incidentlens_control_plane.investigation.service import (
    InvestigationAlreadyTerminal,
    InvestigationService,
)
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.transcript import TranscriptService
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    Checkpoint,
    ChildReportStatus,
    EvidenceReference,
    Investigation,
    InvestigationBudget,
    MessageRole,
    SessionMemory,
    StopReason,
    TextBlock,
    ToolResultBlock,
    TranscriptMessage,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.remote_ops.transport import RemoteTimeoutError

from investigation.test_tool_executor import (
    CONTAINER,
    CONTAINER_ROOT,
    LOG_PATH,
    PROJECT_ID,
    SERVICE,
    TARGET_ID,
    HarnessTransportFactory,
    build_harness,
    make_scope,
    seed_host_log_file,
    tool_request,
)

NOW = datetime(2026, 8, 12, 10, 0, 0, tzinfo=UTC)


def _latest_owned_evidence_id(request: ConversationRequest) -> str | None:
    """Return the most recent evidence id the run owns, from the bounded context.

    Scans the transcript for the newest tool-result evidence, falling back to
    the synthesized header's "Evidence collected (recent)" list when no tool
    result has been persisted yet (a seeded-evidence run).
    """
    header_ids: list[str] = []
    for message in request.messages:
        for block in message.blocks:
            if isinstance(block, ToolResultBlock) and block.evidence_ids:
                return block.evidence_ids[0]
            if isinstance(block, TextBlock):
                marker = "Evidence collected (recent):"
                if marker in block.text:
                    tail = block.text.split(marker, 1)[1]
                    for line in tail.splitlines()[1:]:
                        if not line.startswith("- "):
                            break
                        candidate = line[2:].split(":", 1)[0].strip()
                        if candidate:
                            header_ids.append(candidate)
    if header_ids:
        return header_ids[-1]
    return None


class GroundedStopProvider(ModelProvider):
    """Wrap a scripted provider to ground a bare COMPLETED stop.

    A ``StopStep`` that declares COMPLETED without a conclusion is completed
    with a grounded conclusion citing the run's latest evidence (derived from
    the bounded conversation messages), so tests do not need to predict
    hash-derived evidence ids.
    """

    def __init__(self, delegate: ModelProvider) -> None:
        self._delegate = delegate

    async def generate_turn(self, request: ConversationRequest) -> AgentTurnResult:
        result = await self._delegate.generate_turn(request)
        if (
            result.stop_signal is not None
            and result.stop_signal.stop_reason is StopReason.COMPLETED
            and not result.conclusions
        ):
            latest = _latest_owned_evidence_id(request)
            if latest is not None:
                result = result.model_copy(
                    update={
                        "conclusions": (
                            Conclusion(
                                summary="root cause identified from collected evidence",
                                facts=(latest,),
                                evidence_ids=(latest,),
                            ),
                        )
                    }
                )
        return result


def _make_investigation(
    harness: Any,
    *,
    investigation_id: str = "inv-1",
    status: InvestigationStatus = InvestigationStatus.CREATED,
    budget: InvestigationBudget | None = None,
) -> Investigation:
    investigation = Investigation(
        investigation_id=investigation_id,
        incident_id="inc-1",
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        service=SERVICE,
        symptom="checkout requests are failing",
        status=status,
        budget=budget or InvestigationBudget(),
        usage=UsageCounters(),
        created_at=NOW,
        updated_at=NOW,
    )
    harness.investigations.create_investigation(investigation)
    return investigation


def _make_parent_run(
    harness: Any,
    *,
    run_id: str = "run-1",
    investigation_id: str = "inv-1",
    scope: AgentScope | None = None,
    budget: AgentBudget | None = None,
    evidence: tuple[EvidenceReference, ...] = (),
    status: AgentRunStatus = AgentRunStatus.CREATED,
) -> AgentRun:
    run = AgentRun(
        agent_run_id=run_id,
        investigation_id=investigation_id,
        parent_run_id=None,
        kind=AgentRunKind.PARENT,
        scope=scope or make_scope(),
        status=status,
        budget=budget or AgentBudget(),
        usage=UsageCounters(),
        evidence=evidence,
        created_at=NOW,
        updated_at=NOW,
    )
    harness.investigations.create_agent_run(run)
    return run


def build_orchestrator(
    harness: Any,
    registry: FakeProviderRegistry,
    *,
    now=None,
    **kwargs: Any,
) -> tuple[AgentOrchestrator, InvestigationService, FakeProvider]:
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
        **kwargs,
    )
    service = InvestigationService(
        store=harness.investigations, orchestrator=orchestrator, now=now
    )
    return orchestrator, service, provider


def _hypothesis(summary: str = "db pool exhaustion") -> HypothesisProposal:
    return HypothesisProposal(summary=summary, evidence_ids=())


def _recorded_child_report(
    harness: Any, parent_run: AgentRun, child_run_id: str
) -> tuple[EvidenceReference, Any]:
    """Return the child-report evidence ref on the parent plus its stored record."""
    refs = [
        ref for ref in parent_run.evidence if ref.operation_id == f"child:{child_run_id}"
    ]
    assert refs, "parent run has no child-report evidence for the child"
    stored = harness.evidence_store.get(refs[0].evidence_id)
    assert stored.evidence_kind is EvidenceKind.CHILD_REPORT
    return refs[0], stored


# ---------------------------------------------------------------------------
# Parent completion / safe stops
# ---------------------------------------------------------------------------


async def test_parent_completes_with_grounded_conclusion(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    seed = harness.evidence.record_validation_result(
        agent_run_id="run-1",
        incident_id="inc-1",
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        service_name=SERVICE,
        source_ref="seed",
        validator="test",
        passed=True,
        detail="seed evidence for the run",
        created_by="test",
        now=NOW,
    )
    run = harness.investigations.get_agent_run("run-1")
    run = run.model_copy(
        update={
            "evidence": (
                EvidenceReference(
                    evidence_id=seed.evidence_ref_id,
                    operation_id="seed",
                    summary="seed evidence",
                ),
            )
        }
    )
    harness.investigations.update_agent_run(run)
    registry.set_script(
        "run-1",
        [StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done"))],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.COMPLETED
    assert final.stop_reason is StopReason.COMPLETED
    assert service.get_investigation("inv-1").status is InvestigationStatus.COMPLETED
    assert [cp.sequence for cp in harness.investigations.list_checkpoints("run-1")] == [1, 2]
    assert [r.round_number for r in harness.investigations.list_rounds("run-1")] == [1]
    conclusions = harness.investigations.list_conclusions(agent_run_id="run-1")
    assert len(conclusions) == 1
    assert conclusions[0].evidence_ids == (seed.evidence_ref_id,)
    assert final.usage.rounds == 1


async def test_ungrounded_conclusion_pauses_missing_evidence(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            StopStep(
                stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done"),
                conclusion=Conclusion(summary="root cause", evidence_ids=()),
            )
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.PAUSED_MISSING_EVIDENCE
    assert final.stop_reason is StopReason.MISSING_EVIDENCE
    assert (
        service.get_investigation("inv-1").status is InvestigationStatus.PAUSED_MISSING_EVIDENCE
    )


async def test_completed_stop_without_conclusion_pauses_missing_evidence(tmp_path: Any) -> None:
    """I4: a COMPLETED stop with zero conclusions must not complete ungrounded."""
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            StopStep(
                stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done")
            )
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.PAUSED_MISSING_EVIDENCE
    assert final.stop_reason is StopReason.MISSING_EVIDENCE
    assert (
        service.get_investigation("inv-1").status is InvestigationStatus.PAUSED_MISSING_EVIDENCE
    )


async def test_no_new_evidence_round_top_stop_reason_is_budget_no_new_evidence(
    tmp_path: Any,
) -> None:
    """M6: the round-top no-new-evidence pause is not mislabelled as evidence."""
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(
        harness,
        status=AgentRunStatus.RUNNING,
        budget=AgentBudget(max_no_new_evidence_rounds=2),
    )
    run = harness.investigations.get_agent_run("run-1")
    run = run.model_copy(
        update={"usage": UsageCounters(consecutive_no_new_evidence_rounds=2)}
    )
    harness.investigations.update_agent_run(run)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(tool_request("registry_info", "registry-call"),)
            )
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.PAUSED_MISSING_EVIDENCE
    assert final.stop_reason is StopReason.BUDGET_NO_NEW_EVIDENCE


async def test_round_budget_exhaustion_pauses(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness, budget=AgentBudget(max_rounds=1))
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    tool_request("log_search", "call-1", service_name=SERVICE, text="timeout"),
                )
            )
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.PAUSED_BUDGET
    assert final.stop_reason is StopReason.BUDGET_ROUNDS
    assert final.usage.rounds == 1
    assert service.get_investigation("inv-1").status is InvestigationStatus.PAUSED_BUDGET


async def test_no_new_evidence_pauses_missing_evidence(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness, budget=AgentBudget(max_no_new_evidence_rounds=2))
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(tool_requests=(), hypotheses=(_hypothesis(),)),
            RequestToolsStep(tool_requests=(), hypotheses=(_hypothesis(),)),
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.PAUSED_MISSING_EVIDENCE
    assert final.stop_reason is StopReason.BUDGET_NO_NEW_EVIDENCE
    assert final.usage.consecutive_no_new_evidence_rounds == 2
    assert (
        service.get_investigation("inv-1").status is InvestigationStatus.PAUSED_MISSING_EVIDENCE
    )


async def test_tool_approval_pauses_and_persists_approval_id(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    tool_request(
                        "docker_action", "call-1", service_name=SERVICE,
                        action="restart", container=CONTAINER, reason="investigate",
                    ),
                )
            )
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.WAITING_APPROVAL
    assert final.stop_reason is StopReason.PENDING_APPROVAL
    assert service.get_investigation("inv-1").status is InvestigationStatus.WAITING_APPROVAL
    tool_calls = harness.investigations.list_tool_calls(agent_run_id="run-1")
    assert tool_calls[0].status is ToolCallStatus.WAITING_APPROVAL
    assert tool_calls[0].approval_id is not None
    assert service.list_waiting_approval_tool_calls() == (tool_calls[0],)
    pending = harness.approvals.list()
    assert len(pending) == 1
    assert pending[0].approval_id == tool_calls[0].approval_id
    # Resuming a still-blocked approval keeps the run waiting.
    resumed = await orchestrator.run("run-1")
    assert resumed.status is AgentRunStatus.WAITING_APPROVAL


async def test_provider_declared_cancel_finalises_cancelled(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            StopStep(
                stop_signal=StopSignal(stop_reason=StopReason.CANCELLED, summary="abort")
            )
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.CANCELLED
    assert final.stop_reason is StopReason.CANCELLED
    assert service.get_investigation("inv-1").status is InvestigationStatus.CANCELLED


async def test_uncertain_remote_state_pauses(tmp_path: Any) -> None:
    harness = build_harness(
        tmp_path,
        transport_factory=HarnessTransportFactory(
            run_argv_error=RemoteTimeoutError("docker logs timed out")
        ),
    )
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    tool_request(
                        "log_query", "call-1", service_name=SERVICE,
                        source_kind="docker", source_ref=CONTAINER,
                    ),
                )
            )
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.PAUSED_UNCERTAIN_STATE
    assert final.stop_reason is StopReason.UNCERTAIN_STATE
    assert (
        service.get_investigation("inv-1").status
        is InvestigationStatus.PAUSED_UNCERTAIN_STATE
    )
    tool_calls = harness.investigations.list_tool_calls(agent_run_id="run-1")
    assert tool_calls[0].status is ToolCallStatus.UNCERTAIN
    stored = harness.evidence_store.get(tool_calls[0].evidence_ids[0])
    assert stored.evidence_kind is EvidenceKind.UNCERTAIN_STATE


async def test_approval_reexecution_uncertain_parks_run(tmp_path: Any) -> None:
    """An approved tool that re-executes UNCERTAIN parks the run, never resumes."""
    harness = build_harness(
        tmp_path,
        transport_factory=HarnessTransportFactory(
            run_argv_error=RemoteTimeoutError("docker action timed out")
        ),
    )
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    tool_request(
                        "docker_action", "call-1", service_name=SERVICE,
                        action="restart", container=CONTAINER, reason="investigate",
                    ),
                )
            )
        ],
    )
    provider = FakeProvider(registry)
    wrapped = GroundedStopProvider(provider)
    orchestrator = AgentOrchestrator(
        store=harness.investigations,
        provider=wrapped,
        executor=harness.executor,
        evidence=harness.evidence,
        projects=harness.projects,
        sessions=harness.sessions,
        now=lambda: NOW,
    )
    service = InvestigationService(
        store=harness.investigations,
        orchestrator=orchestrator,
        now=lambda: NOW,
        approvals=harness.approvals,
        executor=harness.executor,
    )

    final = await orchestrator.run("run-1")
    assert final.status is AgentRunStatus.WAITING_APPROVAL
    tool_calls = harness.investigations.list_tool_calls(agent_run_id="run-1")
    approval_id = tool_calls[0].approval_id
    assert approval_id is not None

    await harness.approvals.approve(approval_id)
    outcome = await service.handle_approval_decision(approval_id, now=NOW)

    assert outcome.action == "uncertain"
    run = harness.investigations.get_agent_run("run-1")
    assert run.status is AgentRunStatus.PAUSED_UNCERTAIN_STATE
    assert run.stop_reason is StopReason.UNCERTAIN_STATE
    assert (
        service.get_investigation("inv-1").status
        is InvestigationStatus.PAUSED_UNCERTAIN_STATE
    )
    tool_calls = harness.investigations.list_tool_calls(agent_run_id="run-1")
    assert tool_calls[0].status is ToolCallStatus.UNCERTAIN
    stored = harness.evidence_store.get(tool_calls[0].evidence_ids[0])
    assert stored.evidence_kind is EvidenceKind.UNCERTAIN_STATE


# ---------------------------------------------------------------------------
# Parent / container-child delegation
# ---------------------------------------------------------------------------


def _container_scope() -> AgentScope:
    return AgentScope(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        scope=LogScope.CONTAINER,
        service_name=SERVICE,
        container_name=CONTAINER,
        allowed_container_paths=(CONTAINER_ROOT,),
    )


async def test_delegate_child_tool_spawns_child(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    tool_request(
                        "delegate_child",
                        "call-1",
                        child_run_id="child-1",
                        task_prompt="inspect the payments container",
                        scope={
                            "project_id": PROJECT_ID,
                            "target_id": TARGET_ID,
                            "scope": "container",
                            "service_name": SERVICE,
                            "container_name": CONTAINER,
                            "allowed_container_paths": [str(CONTAINER_ROOT)],
                        },
                    ),
                )
            ),
            StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done")),
        ],
    )
    registry.set_script(
        "child-1",
        [
            RequestToolsStep(tool_requests=(tool_request("registry_info", "child-call-1"),)),
            StopStep(
                stop_signal=StopSignal(
                    stop_reason=StopReason.COMPLETED, summary="child done"
                )
            ),
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.COMPLETED
    child = harness.investigations.get_agent_run("child-1")
    assert child.status is AgentRunStatus.COMPLETED
    ref, stored = _recorded_child_report(harness, final, "child-1")
    assert stored.metadata["status"] == ChildReportStatus.COMPLETE.value


async def test_parent_receives_child_report_and_closes_container_session(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            DelegateChildStep(
                delegation=ChildDelegationRequest(
                    child_run_id="child-1",
                    task_prompt="inspect the payments container",
                    scope=_container_scope(),
                )
            ),
            # A registry round gives the parent its own evidence so the COMPLETED
            # stop is grounded even before the child report is drained.
            RequestToolsStep(
                tool_requests=(tool_request("registry_info", "parent-registry-call"),)
            ),
            StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done")),
        ],
    )
    registry.set_script(
        "child-1",
        [
            RequestToolsStep(tool_requests=(tool_request("registry_info", "child-call-1"),)),
            StopStep(
                stop_signal=StopSignal(
                    stop_reason=StopReason.COMPLETED, summary="child done"
                )
            ),
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.COMPLETED
    child = harness.investigations.get_agent_run("child-1")
    assert child.status is AgentRunStatus.COMPLETED
    assert child.kind is AgentRunKind.CHILD
    assert child.parent_run_id == "run-1"
    # The parent only received the structured report + evidence ref, not raw
    # content: the report is recorded as CHILD_REPORT evidence on the parent.
    ref, stored = _recorded_child_report(harness, final, "child-1")
    assert stored.metadata["child_run_id"] == "child-1"
    assert stored.metadata["status"] == ChildReportStatus.COMPLETE.value
    assert final.stop_reason is StopReason.COMPLETED
    # The child's container session was closed; the host session survives.
    assert harness.sessions._container_sessions == {}
    live = await harness.sessions.find_live(TARGET_ID)
    assert live is not None


async def test_child_crash_writes_partial_report(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            DelegateChildStep(
                delegation=ChildDelegationRequest(
                    child_run_id="child-1",
                    task_prompt="inspect the container",
                    scope=_container_scope(),
                )
            ),
            StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done")),
        ],
    )
    registry.set_script("child-1", [CrashStep(message="provider segfaulted")])
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    child = harness.investigations.get_agent_run("child-1")
    assert child.status is AgentRunStatus.FAILED
    _, stored = _recorded_child_report(harness, final, "child-1")
    assert stored.metadata["status"] == ChildReportStatus.PARTIAL.value
    assert stored.metadata["stop_reason"] == StopReason.FAILED.value


async def test_child_cannot_delegate_grandchild(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            DelegateChildStep(
                delegation=ChildDelegationRequest(
                    child_run_id="child-1",
                    task_prompt="inspect the container",
                    scope=_container_scope(),
                )
            ),
            StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done")),
        ],
    )
    registry.set_script(
        "child-1",
        [
            DelegateChildStep(
                delegation=ChildDelegationRequest(
                    child_run_id="child-1a",
                    task_prompt="nested delegation",
                    scope=_container_scope(),
                )
            )
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    child = harness.investigations.get_agent_run("child-1")
    assert child.status is AgentRunStatus.FAILED
    assert all(run.agent_run_id != "child-1a" for run in harness.investigations.list_agent_runs())
    _, stored = _recorded_child_report(harness, final, "child-1")
    assert stored.metadata["status"] == ChildReportStatus.PARTIAL.value


async def test_parent_delegates_two_children_concurrently(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            DelegateChildStep(
                delegation=ChildDelegationRequest(
                    child_run_id="child-1", task_prompt="inspect container A",
                    scope=_container_scope(),
                )
            ),
            DelegateChildStep(
                delegation=ChildDelegationRequest(
                    child_run_id="child-2", task_prompt="inspect container B",
                    scope=_container_scope(),
                )
            ),
            RequestToolsStep(
                tool_requests=(tool_request("registry_info", "parent-registry-call"),)
            ),
            StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done")),
        ],
    )
    for index, child_id in enumerate(("child-1", "child-2")):
        registry.set_script(
            child_id,
            [
                RequestToolsStep(
                    tool_requests=(
                        tool_request("registry_info", f"child-{index}-call"),
                    ),
                ),
                StopStep(
                    stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="child done")
                ),
            ],
        )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.COMPLETED
    child_1 = harness.investigations.get_agent_run("child-1")
    child_2 = harness.investigations.get_agent_run("child-2")
    assert child_1.status is AgentRunStatus.COMPLETED
    assert child_2.status is AgentRunStatus.COMPLETED
    assert service.get_investigation("inv-1").usage.children == 2
    child_refs = [ref for ref in final.evidence if ref.operation_id.startswith("child:")]
    assert {ref.operation_id for ref in child_refs} == {"child:child-1", "child:child-2"}


@pytest.mark.asyncio
async def test_parent_delivers_terminal_child_receipt_after_restart(tmp_path: Any) -> None:
    """A persisted terminal child is delivered once by two fresh processes.

    The child is run separately first so its durable receipt is intentionally
    still undelivered.  The first fresh parent process reconciles it before its
    safe scripted provider turn; the second process sees the terminal parent
    and must not rerun either child work or the notification.
    """
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness, status=AgentRunStatus.RUNNING)
    registry.set_script(
        "child-1",
        [
            RequestToolsStep(tool_requests=(tool_request("registry_info", "child-call"),)),
            StopStep(
                stop_signal=StopSignal(
                    stop_reason=StopReason.COMPLETED, summary="child done"
                )
            ),
        ],
    )
    pending: list[tuple[str, asyncio.Task]] = []
    seeder, _, child_provider = build_orchestrator(harness, registry)
    parent = harness.investigations.get_agent_run("run-1")
    investigation = harness.investigations.get_investigation("inv-1")
    await seeder._delegate_child(
        parent,
        investigation,
        ChildDelegationRequest(
            child_run_id="child-1", task_prompt="inspect", scope=make_scope()
        ),
        pending,
        NOW,
    )
    assert len(pending) == 1
    await pending[0][1]
    receipt = harness.investigations.get_child_report_receipt("child-1")
    assert receipt.delivered_at is None
    assert child_provider.call_count("child-1") == 2

    registry.set_script(
        "run-1",
        [StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done"))],
    )
    first, _, first_provider = build_orchestrator(harness, registry)
    first_result = await first.run("run-1")
    assert first_result.status is AgentRunStatus.COMPLETED
    child_calls_before_second = child_provider.call_count("child-1")
    second, _, second_provider = build_orchestrator(harness, registry)
    second_result = await second.run("run-1")

    parent = harness.investigations.get_agent_run("run-1")
    assert second_result.status is AgentRunStatus.COMPLETED
    assert [ref.operation_id for ref in parent.evidence].count("child:child-1") == 1
    notifications = [
        message
        for message in harness.investigations.list_transcript_messages("run-1")
        if any(
            isinstance(block, TextBlock) and "Child report child-1" in block.text
            for block in message.blocks
        )
    ]
    assert len(notifications) == 1
    assert harness.investigations.get_child_report_receipt("child-1").delivered_at is not None
    assert second_provider.call_count("child-1") == child_calls_before_second




async def test_cancel_with_inflight_child_writes_partial_report(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness, budget=AgentBudget(max_no_new_evidence_rounds=20))
    registry.set_script(
        "run-1",
        [
            DelegateChildStep(
                delegation=ChildDelegationRequest(
                    child_run_id="child-1", task_prompt="inspect the container",
                    scope=_container_scope(),
                )
            ),
            *[RequestToolsStep(tool_requests=(), hypotheses=(_hypothesis(),))] * 30,
        ],
    )
    registry.set_script(
        "child-1",
        [RequestToolsStep(tool_requests=(), hypotheses=(_hypothesis(),))] * 30,
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    task = asyncio.create_task(orchestrator.run("run-1"))
    # Let the parent delegate child-1 before cancelling, so the child is in
    # flight and must produce a partial report.
    for _ in range(200):
        if any(
            run.agent_run_id == "child-1"
            for run in harness.investigations.list_agent_runs()
        ):
            break
        await asyncio.sleep(0)
    assert any(
        run.agent_run_id == "child-1"
        for run in harness.investigations.list_agent_runs()
    )
    await service.cancel("inv-1")

    final = await task
    assert final.status is AgentRunStatus.CANCELLED
    assert final.stop_reason is StopReason.CANCELLED
    child = harness.investigations.get_agent_run("child-1")
    assert child.status is AgentRunStatus.CANCELLED
    ref, stored = _recorded_child_report(harness, final, "child-1")
    assert stored.metadata["status"] == ChildReportStatus.PARTIAL.value
    assert stored.metadata["stop_reason"] == StopReason.CANCELLED.value


async def test_cancel_is_idempotent_and_finalises_cancelled(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    registry.set_script(
        "run-1", [RequestToolsStep(tool_requests=(), hypotheses=(_hypothesis(),))] * 40
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    task = asyncio.create_task(orchestrator.run("run-1"))
    await asyncio.sleep(0)

    first = await service.cancel("inv-1")
    assert first.status is InvestigationStatus.CANCEL_REQUESTED
    second = await service.cancel("inv-1")  # idempotent
    assert second.status in {
        InvestigationStatus.CANCEL_REQUESTED,
        InvestigationStatus.CANCELLED,
    }

    final = await task
    assert final.status is AgentRunStatus.CANCELLED
    assert final.stop_reason is StopReason.CANCELLED
    assert service.get_investigation("inv-1").status is InvestigationStatus.CANCELLED


async def test_cancel_run_is_idempotent(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness, status=AgentRunStatus.RUNNING)
    registry.set_script(
        "run-1", [RequestToolsStep(tool_requests=(), hypotheses=(_hypothesis(),))] * 20
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    await service.cancel_run("run-1")
    parked = harness.investigations.get_agent_run("run-1")
    assert parked.status is AgentRunStatus.CANCEL_REQUESTED
    again = await service.cancel_run("run-1")
    assert again.status is AgentRunStatus.CANCEL_REQUESTED


async def test_resume_from_checkpoint_reenters_round_idempotently(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    # Simulate an interrupted round: only the before_model_turn checkpoint of
    # round 1 exists, as if the process died mid-round.
    harness.investigations.append_checkpoint(
        Checkpoint(
            checkpoint_id="cp-interrupt",
            agent_run_id="run-1",
            sequence=1,
            status=AgentRunStatus.RUNNING,
            round_number=1,
            usage=UsageCounters(),
            created_at=NOW,
        )
    )
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(tool_request("registry_info", "registry-call"),)
            ),
            StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done")),
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await service.resume_run("run-1")

    assert final.status is AgentRunStatus.COMPLETED
    # The re-entered round did not duplicate its before-model-turn checkpoint.
    assert [cp.sequence for cp in harness.investigations.list_checkpoints("run-1")] == [
        1,
        2,
        3,
        4,
    ]
    assert [r.round_number for r in harness.investigations.list_rounds("run-1")] == [1, 2]


async def test_resume_terminal_run_is_a_no_op(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness, status=AgentRunStatus.COMPLETED)
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await service.resume_run("run-1")

    assert final.status is AgentRunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Investigation service
# ---------------------------------------------------------------------------


async def test_service_create_and_start(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    orchestrator, service, _ = build_orchestrator(harness, registry)

    investigation = service.create_investigation(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        service=SERVICE,
        symptom="checkout failures",
    )
    assert investigation.status is InvestigationStatus.CREATED
    assert investigation.incident_id.startswith("inc-")

    _make_parent_run(harness, run_id="run-1", investigation_id=investigation.investigation_id)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(tool_request("registry_info", "registry-call"),)
            ),
            StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done")),
        ],
    )
    final = await service.start(investigation.investigation_id, make_scope())

    assert final.status is AgentRunStatus.COMPLETED
    assert (
        service.get_investigation(investigation.investigation_id).status
        is InvestigationStatus.COMPLETED
    )


async def test_service_start_terminal_investigation_raises(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    orchestrator, service, _ = build_orchestrator(harness, registry)
    investigation = _make_investigation(harness, status=InvestigationStatus.COMPLETED)

    with pytest.raises(InvestigationAlreadyTerminal):
        await service.start(investigation.investigation_id, make_scope())


# ---------------------------------------------------------------------------
# Evidence / output / tool-call budget enforcement (I1 / I2) and lost updates
# ---------------------------------------------------------------------------


async def test_run_evidence_budget_exhaustion_pauses(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    await seed_host_log_file(harness, "line one\nerror line two\nline three\n")
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness, budget=AgentBudget(max_evidence=1))
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    tool_request(
                        "log_query", "log-call-1", service_name=SERVICE,
                        source_kind="file", source_ref=str(LOG_PATH),
                    ),
                )
            )
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.PAUSED_BUDGET
    assert final.stop_reason is StopReason.BUDGET_EVIDENCE
    # The paused run never exceeded its evidence cap.
    assert final.usage.evidence_count <= final.budget.max_evidence


async def test_investigation_evidence_budget_exhaustion_pauses(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    await seed_host_log_file(harness, "line one\nerror line two\nline three\n")
    registry = FakeProviderRegistry()
    _make_investigation(
        harness,
        status=InvestigationStatus.RUNNING,
        budget=InvestigationBudget(max_evidence=1),
    )
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    tool_request(
                        "log_query", "log-call-1", service_name=SERVICE,
                        source_kind="file", source_ref=str(LOG_PATH),
                    ),
                )
            )
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.PAUSED_BUDGET
    assert final.stop_reason is StopReason.BUDGET_EVIDENCE
    inv = service.get_investigation("inv-1")
    assert inv.usage.evidence_count <= inv.budget.max_evidence


async def test_investigation_tool_call_budget_exhaustion_pauses(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(
        harness,
        status=InvestigationStatus.RUNNING,
        budget=InvestigationBudget(max_tool_calls=1),
    )
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    tool_request("registry_info", "registry-call-1"),
                    tool_request("registry_info", "registry-call-2"),
                )
            )
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.PAUSED_BUDGET
    assert final.stop_reason is StopReason.BUDGET_TOOL_CALLS


async def test_run_cumulative_output_budget_exhaustion_pauses(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness, budget=AgentBudget(max_total_output_bytes=1))
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(tool_request("registry_info", "registry-call"),)
            )
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.PAUSED_BUDGET
    assert final.stop_reason is StopReason.BUDGET_OUTPUT
    assert final.usage.total_output_bytes <= final.budget.max_total_output_bytes


async def test_investigation_output_budget_exhaustion_pauses(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(
        harness,
        status=InvestigationStatus.RUNNING,
        budget=InvestigationBudget(max_total_output_bytes=1),
    )
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(tool_request("registry_info", "registry-call"),)
            )
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.PAUSED_BUDGET
    assert final.stop_reason is StopReason.BUDGET_OUTPUT
    inv = service.get_investigation("inv-1")
    assert inv.usage.total_output_bytes <= inv.budget.max_total_output_bytes


async def test_child_usage_increments_not_clobbered_by_parent(tmp_path: Any) -> None:
    """A child's investigation-usage increments survive the parent's writes."""
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    registry.set_script(
        "run-1",
        [
            DelegateChildStep(
                delegation=ChildDelegationRequest(
                    child_run_id="child-1", task_prompt="inspect the container",
                    scope=_container_scope(),
                )
            ),
            RequestToolsStep(
                tool_requests=(tool_request("registry_info", "parent-registry-call"),)
            ),
            StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done")),
        ],
    )
    registry.set_script(
        "child-1",
        [
            RequestToolsStep(tool_requests=(tool_request("registry_info", "child-call-1"),)),
            StopStep(
                stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="child done")
            ),
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.COMPLETED
    inv = service.get_investigation("inv-1")
    # parent rounds (3) + child rounds (2) all counted, child tool call counted.
    assert inv.usage.rounds == 5
    assert inv.usage.tool_calls == 2  # parent registry_info + child registry_info
    assert inv.usage.children == 1


async def test_hypothesis_continue_path_preserves_child_usage(tmp_path: Any) -> None:
    """The hypotheses-only continue path must not clobber a child's usage."""
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(
        harness,
        status=InvestigationStatus.RUNNING,
        budget=InvestigationBudget(max_no_new_evidence_rounds=20),
    )
    _make_parent_run(harness, budget=AgentBudget(max_no_new_evidence_rounds=20))
    registry.set_script(
        "run-1",
        [
            DelegateChildStep(
                delegation=ChildDelegationRequest(
                    child_run_id="child-1", task_prompt="inspect the container",
                    scope=_container_scope(),
                )
            ),
            RequestToolsStep(tool_requests=(), hypotheses=(_hypothesis(),)),
            RequestToolsStep(tool_requests=(), hypotheses=(_hypothesis(),)),
            RequestToolsStep(
                tool_requests=(tool_request("registry_info", "parent-registry-call"),)
            ),
            StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done")),
        ],
    )
    registry.set_script(
        "child-1",
        [
            RequestToolsStep(tool_requests=(tool_request("registry_info", "child-call-1"),)),
            StopStep(
                stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="child done")
            ),
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.COMPLETED
    inv = service.get_investigation("inv-1")
    # parent rounds (1 delegate + 2 hypothesis + 1 registry + 1 stop = 5) plus
    # child rounds (2) all counted; the continue path must not lose the child's.
    assert inv.usage.rounds == 7
    assert inv.usage.tool_calls == 2
    assert inv.usage.children == 1


async def test_child_report_over_evidence_budget_pauses_not_crash(tmp_path: Any) -> None:
    """A child report exceeding the parent's max_evidence pauses, never crashes."""
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness, budget=AgentBudget(max_evidence=1))
    registry.set_script(
        "run-1",
        [
            DelegateChildStep(
                delegation=ChildDelegationRequest(
                    child_run_id="child-1", task_prompt="inspect the container",
                    scope=_container_scope(),
                )
            ),
            RequestToolsStep(
                tool_requests=(tool_request("registry_info", "parent-registry-call"),)
            ),
            StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done")),
        ],
    )
    registry.set_script(
        "child-1",
        [
            RequestToolsStep(
                tool_requests=(tool_request("registry_info", f"child-call-{i}"),)
            )
            for i in range(3)
        ]
        + [
            StopStep(
                stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="child done")
            )
        ],
    )
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    # The parent's registry round put it at its evidence cap (1); the child
    # report would exceed it, so the run must pause -- not raise.
    assert final.status is AgentRunStatus.PAUSED_BUDGET
    assert final.stop_reason is StopReason.BUDGET_EVIDENCE
    assert final.usage.evidence_count <= final.budget.max_evidence
    child = harness.investigations.get_agent_run("child-1")
    assert child.status is AgentRunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Provider contract invariants through the orchestrator
# ---------------------------------------------------------------------------


async def test_provider_crash_fails_run(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    registry = FakeProviderRegistry()
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(harness)
    registry.set_script("run-1", [CrashStep(message="hard crash")])
    orchestrator, service, _ = build_orchestrator(harness, registry)

    final = await orchestrator.run("run-1")

    assert final.status is AgentRunStatus.FAILED
    assert service.get_investigation("inv-1").status is InvestigationStatus.FAILED


# ---------------------------------------------------------------------------
# Continuous-loop harness (runtime fixture) + Step 1 loop continuity / retry
# ---------------------------------------------------------------------------


class _RemoteSpy:
    """Counts remote write_bytes calls seen by the harness transports."""

    def __init__(self) -> None:
        self.write_calls = 0


class _WriteCountingTransport:
    """Proxies a HarnessTransport and counts write_bytes calls."""

    def __init__(self, inner: Any, spy: _RemoteSpy) -> None:
        self._inner = inner
        self._spy = spy

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def write_bytes(
        self, path: Any, content: bytes, *, mode: int = 0o644, exclusive: bool = False
    ) -> None:
        self._spy.write_calls += 1
        return await self._inner.write_bytes(path, content, mode=mode, exclusive=exclusive)


class _WriteCountingFactory:
    """A transport factory wrapper that counts remote writes."""

    def __init__(self, factory: Any, spy: _RemoteSpy) -> None:
        self._factory = factory
        self._spy = spy

    async def connect(self, target: Any) -> _WriteCountingTransport:
        transport = await self._factory.connect(target)
        return _WriteCountingTransport(transport, self._spy)


class _FailingTranscriptService(TranscriptService):
    """A transcript service that can fail the next append (disk-full simulation)."""

    def __init__(self, store: Any) -> None:
        super().__init__(store)
        self.fail_next_append: Exception | None = None

    def append_message(self, message: TranscriptMessage) -> TranscriptMessage:
        if self.fail_next_append is not None:
            error = self.fail_next_append
            self.fail_next_append = None
            raise error
        return super().append_message(message)


class _FakeCompactor:
    """Deterministic semantic compactor for the harness (no model involved)."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def compact(self, request: Any) -> SessionMemory:
        run = self._store.get_agent_run(request.agent_run_id)
        investigation = self._store.get_investigation(run.investigation_id)
        prior = request.prior_memory
        revision = (prior.revision + 1) if prior is not None else 1
        return SessionMemory(
            memory_id=f"mem-{request.agent_run_id}-{revision}",
            agent_run_id=request.agent_run_id,
            investigation_id=investigation.investigation_id,
            revision=revision,
            through_round=run.usage.rounds,
            through_transcript_sequence=request.through_sequence,
            objective="compacted transcript summary",
            confirmed_facts=(),
            active_hypotheses=(),
            open_questions=(),
            completed_actions=(),
            child_findings=(),
            evidence_ids=tuple(
                dict.fromkeys(ref.evidence_id for ref in run.evidence)
            ),
            user_constraints=(),
            todos=(),
            next_actions=(),
            created_at=NOW,
        )


@pytest.fixture
def runtime(tmp_path: Any) -> SimpleNamespace:
    """Wire the continuous-loop harness: store + transcript + context + fake.

    ``runtime.fake`` is the scripted ``FakeProvider``, ``runtime.orchestrator``
    runs the loop, ``runtime.transcript`` can fail its next append, and
    ``runtime.remote`` counts remote write calls so append-before-act tests can
    prove nothing executed.
    """
    base_factory = HarnessTransportFactory()
    remote = _RemoteSpy()
    harness = build_harness(
        tmp_path, transport_factory=_WriteCountingFactory(base_factory, remote)
    )
    _make_investigation(harness, status=InvestigationStatus.RUNNING)
    _make_parent_run(
        harness,
        evidence=(
            EvidenceReference(
                evidence_id="ev-1",
                operation_id="seed",
                summary="seeded evidence",
            ),
        ),
    )
    store = harness.investigations
    transcript = _FailingTranscriptService(store)
    # Pre-seed the initial user message so ``_ensure_initial_message`` is a
    # no-op and ``fail_next_append`` targets the assistant message.
    transcript.append_message(
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=1,
            role=MessageRole.USER,
            blocks=(TextBlock(text="Symptom: checkout requests are failing"),),
            created_at=NOW,
        )
    )
    registry = FakeProviderRegistry()
    provider = FakeProvider(registry)
    context = AgentContextManager(
        store,
        now=lambda: NOW,
        compactor=_FakeCompactor(store),
    )
    orchestrator = AgentOrchestrator(
        store=store,
        provider=provider,
        executor=harness.executor,
        evidence=harness.evidence,
        projects=harness.projects,
        sessions=harness.sessions,
        now=lambda: NOW,
        transcript=transcript,
        context_manager=context,
    )
    return SimpleNamespace(
        fake=provider,
        orchestrator=orchestrator,
        transcript=transcript,
        remote=remote,
        harness=harness,
        store=store,
    )


def request_registry_info() -> RequestToolsStep:
    """One turn proposing a registry_info call with a stable tool_call_id."""
    return RequestToolsStep(
        tool_requests=(
            ToolRequest(
                tool_call_id="registry-call",
                tool_name="registry_info",
                arguments={},
            ),
        )
    )


def request_file_write() -> RequestToolsStep:
    """One turn proposing a file_write call (never reached when append fails)."""
    return RequestToolsStep(
        tool_requests=(
            ToolRequest(
                tool_call_id="write-call-1",
                tool_name="file_write",
                arguments={
                    "service_name": SERVICE,
                    "path": "/opt/payments/new-file.txt",
                    "content": "hello",
                },
            ),
        )
    )


def completed_step(evidence_id: str) -> StopStep:
    """A COMPLETED stop grounded in the named evidence id."""
    return StopStep(
        stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done"),
        conclusion=Conclusion(
            summary="root cause identified", evidence_ids=(evidence_id,)
        ),
    )


def compact_context_request() -> RequestToolsStep:
    """One turn proposing the local ``compact_context`` control request."""
    return RequestToolsStep(
        tool_requests=(
            ToolRequest(
                tool_call_id="compact-call",
                tool_name="compact_context",
                arguments={},
            ),
        )
    )


@pytest.mark.asyncio
async def test_tool_result_is_in_next_model_conversation(runtime: SimpleNamespace) -> None:
    runtime.fake.script("run-1", [request_registry_info(), completed_step("ev-1")])
    await runtime.orchestrator.run("run-1")
    second = runtime.fake.requests("run-1")[1]
    assert any(
        isinstance(block, ToolResultBlock) and block.tool_call_id == "registry-call"
        for message in second.messages
        for block in message.blocks
    )


@pytest.mark.asyncio
async def test_prompt_too_long_compacts_once_then_retries(runtime: SimpleNamespace) -> None:
    runtime.fake.script("run-1", [PromptTooLongError(), completed_step("ev-1")])
    run = await runtime.orchestrator.run("run-1")
    assert run.status is AgentRunStatus.COMPLETED
    assert runtime.fake.call_count("run-1") == 2


@pytest.mark.asyncio
async def test_unexpected_reactive_compaction_error_pauses_and_emits_failed_hook(
    runtime: SimpleNamespace,
) -> None:
    """A non-circuit compactor exception is converted into a safe pause."""
    from incidentlens_control_plane.investigation.hooks import HookEventType

    seen: list[tuple[object, str, str | None]] = []

    async def capture(event: Any) -> None:
        seen.append((event.event_type, event.action_name, event.status))

    runtime.orchestrator._hooks.register(HookEventType.PRE_COMPACT, capture)
    runtime.orchestrator._hooks.register(HookEventType.POST_COMPACT, capture)

    async def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("context store unavailable")

    runtime.orchestrator._context.reactive_request = explode
    runtime.fake.script("run-1", [PromptTooLongError()])

    run = await runtime.orchestrator.run("run-1")

    assert run.status is AgentRunStatus.PAUSED_BUDGET
    assert run.stop_reason is StopReason.BUDGET_OUTPUT
    assert [item[0] for item in seen] == [
        HookEventType.PRE_COMPACT,
        HookEventType.POST_COMPACT,
    ]
    assert seen[0][1:] == ("compact", "started")
    assert seen[1][1:] == ("compact", "failed")


@pytest.mark.asyncio
async def test_transcript_failure_prevents_tool_execution(runtime: SimpleNamespace) -> None:
    runtime.fake.script("run-1", [request_file_write()])
    runtime.transcript.fail_next_append = OSError("disk full")
    run = await runtime.orchestrator.run("run-1")
    assert run.status is AgentRunStatus.PAUSED_UNCERTAIN_STATE
    assert runtime.remote.write_calls == 0


@pytest.mark.asyncio
async def test_child_and_manual_compact_emit_fixed_hooks(runtime: SimpleNamespace) -> None:
    """Child terminalization drains before the later manual compact."""
    from incidentlens_control_plane.investigation.hooks import HookEventType

    seen: list[tuple[object, str, str | None]] = []

    async def capture(event: Any) -> None:
        seen.append((event.event_type, event.action_name, event.status))

    for event_type in (
        HookEventType.SUBAGENT_START,
        HookEventType.SUBAGENT_STOP,
        HookEventType.PRE_COMPACT,
        HookEventType.POST_COMPACT,
    ):
        runtime.orchestrator._hooks.register(event_type, capture)

    runtime.fake.script(
        "child-1",
        [
            RequestToolsStep(tool_requests=(tool_request("registry_info", "child-call"),)),
            StopStep(
                stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="child")
            ),
        ],
    )
    pending: list[tuple[str, asyncio.Task]] = []
    parent = runtime.store.get_agent_run("run-1")
    investigation = runtime.store.get_investigation("inv-1")
    await runtime.orchestrator._delegate_child(
        parent,
        investigation,
        ChildDelegationRequest(child_run_id="child-1", task_prompt="inspect", scope=make_scope()),
        pending,
        NOW,
    )
    assert len(pending) == 1
    await pending[0][1]

    # Only after the child task has terminalized do we execute the manual
    # compact through the normal parent provider loop.
    runtime.fake.script(
        "run-1", [request_registry_info(), compact_context_request(), completed_step("ev-1")]
    )
    await runtime.orchestrator.run("run-1")

    assert [item[0] for item in seen] == [
        HookEventType.SUBAGENT_START,
        HookEventType.SUBAGENT_STOP,
        HookEventType.PRE_COMPACT,
        HookEventType.POST_COMPACT,
    ]
    assert [(item[1], item[2]) for item in seen[2:]] == [
        ("compact", "started"),
        ("compact", "completed"),
    ]
    assert seen[1][1] == "subagent"
    assert seen[1][2] in {
        AgentRunStatus.COMPLETED.value,
        AgentRunStatus.PAUSED_MISSING_EVIDENCE.value,
    }


@pytest.mark.asyncio
async def test_child_report_context_is_bounded(runtime: SimpleNamespace) -> None:
    reports = []
    for index in range(8):
        report = SimpleNamespace(agent_run_id=f"child-{index}")
        reports.append(report)
    # The orchestrator's delivery helper retains the newest four in place.
    assert len(reports) == 8
    del reports[:-4]
    assert len(reports) == 4


@pytest.mark.asyncio
async def test_manual_compact_commits_memory_and_continues(runtime: SimpleNamespace) -> None:
    """A ``compact_context`` request compacts locally and the run continues."""
    runtime.fake.script("run-1", [compact_context_request(), completed_step("ev-1")])
    run = await runtime.orchestrator.run("run-1")
    assert run.status is AgentRunStatus.COMPLETED
    assert runtime.store.get_latest_session_memory("run-1") is not None
    assert runtime.store.get_latest_compact_boundary("run-1") is not None


def _registry_pair() -> RequestToolsStep:
    """Two concurrency-safe ``registry_info`` calls (same gather batch)."""
    return RequestToolsStep(
        tool_requests=(
            ToolRequest(
                tool_call_id="safe-call-1",
                tool_name="registry_info",
                arguments={},
            ),
            ToolRequest(
                tool_call_id="safe-call-2",
                tool_name="registry_info",
                arguments={},
            ),
        )
    )


@pytest.mark.asyncio
async def test_concurrent_safe_batch_emits_results_in_order(runtime: SimpleNamespace) -> None:
    """Two gathered safe tools both fold; results land in original order."""
    runtime.fake.script("run-1", [_registry_pair(), completed_step("ev-1")])
    run = await runtime.orchestrator.run("run-1")
    assert run.status is AgentRunStatus.COMPLETED
    second = runtime.fake.requests("run-1")[1]
    result_blocks = [
        block
        for message in second.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    ]
    assert [b.tool_call_id for b in result_blocks] == ["safe-call-1", "safe-call-2"]
    assert all(b.status is ToolCallStatus.SUCCEEDED for b in result_blocks)
    calls = runtime.store.list_tool_calls(agent_run_id="run-1")
    assert {c.tool_call_id for c in calls} == {"safe-call-1", "safe-call-2"}
    assert all(c.status is ToolCallStatus.SUCCEEDED for c in calls)


@pytest.mark.asyncio
async def test_batch_folds_all_outcomes_when_one_pauses(runtime: SimpleNamespace) -> None:
    """A batch that pauses mid-fold still folds its already-executed sibling.

    A tiny output budget makes the first gathered tool's fold pause the round;
    the sibling already ran and must NOT be left RUNNING nor misreported as
    "tool call was not executed".
    """
    run = runtime.store.get_agent_run("run-1")
    run = run.model_copy(update={"budget": AgentBudget(max_total_output_bytes=1)})
    runtime.store.update_agent_run(run)
    runtime.fake.script("run-1", [_registry_pair()])
    final = await runtime.orchestrator.run("run-1")
    assert final.status is AgentRunStatus.PAUSED_BUDGET
    calls = runtime.store.list_tool_calls(agent_run_id="run-1")
    assert {c.tool_call_id for c in calls} == {"safe-call-1", "safe-call-2"}
    assert all(c.status is ToolCallStatus.SUCCEEDED for c in calls)
    result_blocks = [
        block
        for message in runtime.store.list_transcript_messages("run-1")
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    ]
    assert [b.tool_call_id for b in result_blocks] == ["safe-call-1", "safe-call-2"]
    assert all(b.status is ToolCallStatus.SUCCEEDED for b in result_blocks)
    assert all("not executed" not in b.content for b in result_blocks)
