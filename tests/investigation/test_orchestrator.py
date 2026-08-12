"""Tests for the bounded parent/container-child agent orchestrator and service.

The harness reuses the real SQLite-backed runtime from ``test_tool_executor``
(project registry, log/evidence/approval stores, gateway, sessions) and drives
the orchestrator with the scripted ``FakeProvider`` so every scenario walks the
real guard / validator / executor / approval paths.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from incidentlens_control_plane.evidence.types import EvidenceKind
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
    AgentTurnRequest,
    AgentTurnResult,
    ChildDelegationRequest,
    Conclusion,
    HypothesisProposal,
    ModelProvider,
    StopSignal,
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
    StopReason,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.remote_ops.transport import RemoteTimeoutError

from investigation.test_tool_executor import (
    CONTAINER,
    CONTAINER_ROOT,
    PROJECT_ID,
    SERVICE,
    TARGET_ID,
    HarnessTransportFactory,
    build_harness,
    make_scope,
    tool_request,
)

NOW = datetime(2026, 8, 12, 10, 0, 0, tzinfo=UTC)


class GroundedStopProvider(ModelProvider):
    """Wrap a scripted provider to ground a bare COMPLETED stop.

    A ``StopStep`` that declares COMPLETED without a conclusion is completed
    with a grounded conclusion citing the run's latest evidence (from the
    bounded request context), so tests do not need to predict hash-derived
    evidence ids.
    """

    def __init__(self, delegate: ModelProvider) -> None:
        self._delegate = delegate

    async def generate_turn(self, request: AgentTurnRequest) -> AgentTurnResult:
        result = await self._delegate.generate_turn(request)
        if (
            result.stop_signal is not None
            and result.stop_signal.stop_reason is StopReason.COMPLETED
            and not result.conclusions
            and request.evidence
        ):
            latest = request.evidence[-1]
            result = result.model_copy(
                update={
                    "conclusions": (
                        Conclusion(
                            summary="root cause identified from collected evidence",
                            facts=(latest.summary[:200],),
                            evidence_ids=(latest.evidence_id,),
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
            RequestToolsStep(tool_requests=(tool_request("registry_info", "call-1"),)),
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
            StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done")),
        ],
    )
    registry.set_script(
        "child-1",
        [
            RequestToolsStep(tool_requests=(tool_request("registry_info", "call-1"),)),
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
            StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done")),
        ],
    )
    for child_id in ("child-1", "child-2"):
        registry.set_script(
            child_id,
            [
                RequestToolsStep(tool_requests=(tool_request("registry_info", "call-1"),)),
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


# ---------------------------------------------------------------------------
# Cancellation / resume
# ---------------------------------------------------------------------------


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
                tool_requests=(tool_request("log_search", "call-1",
                                            service_name=SERVICE, text="timeout"),)
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
        [StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done"))],
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
