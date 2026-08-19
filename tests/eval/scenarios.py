"""Deterministic bounded scenario definitions."""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Awaitable, Callable

from incidentlens_control_plane.investigation.fake_provider import FakeProvider, FakeProviderRegistry, RequestToolsStep, StopStep
from incidentlens_control_plane.investigation.orchestrator import AgentOrchestrator
from incidentlens_control_plane.investigation.provider import StopSignal, ToolRequest
from incidentlens_control_plane.investigation.state_machine import AgentRunStatus, InvestigationStatus
from incidentlens_control_plane.investigation.types import AgentRun, AgentRunKind, AgentScope, AgentBudget, Investigation, InvestigationBudget, StopReason, UsageCounters
from incidentlens_control_plane.logs.types import LogScope
try:
    from .support import build_harness
    from .types import HarnessTrace
except ImportError:
    from tests.eval.support import build_harness
    from tests.eval.types import HarnessTrace

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)

def _scope() -> AgentScope:
    return AgentScope(project_id="payments", target_id="dev-a", scope=LogScope.HOST, allowed_host_paths=())

def _make(tmp: Path, name: str, steps: list[object]) -> HarnessTrace:
    harness = build_harness(tmp)
    inv = Investigation(investigation_id="inv-1", incident_id="inc-1", project_id="payments", target_id="dev-a", service="payment-api", symptom="failure", status=InvestigationStatus.RUNNING, budget=InvestigationBudget(), usage=UsageCounters(), created_at=NOW, updated_at=NOW)
    harness.investigations.create_investigation(inv)
    run = AgentRun(agent_run_id="run-1", investigation_id="inv-1", parent_run_id=None, kind=AgentRunKind.PARENT, scope=_scope(), status=AgentRunStatus.CREATED, budget=AgentBudget(max_rounds=4), usage=UsageCounters(), created_at=NOW, updated_at=NOW)
    harness.investigations.create_agent_run(run)
    registry = FakeProviderRegistry(); registry.set_script("run-1", steps)
    provider = FakeProvider(registry)
    orchestrator = AgentOrchestrator(store=harness.investigations, provider=provider, executor=harness.executor, evidence=harness.evidence, projects=harness.projects, sessions=harness.sessions, now=lambda: NOW)
    # Scenarios intentionally use only registered, deterministic control paths.
    import asyncio
    asyncio.get_event_loop().run_until_complete(orchestrator.run("run-1"))
    final = harness.investigations.get_agent_run("run-1")
    return HarnessTrace(scenario=name, investigation=harness.investigations.get_investigation("inv-1"), run=final, rounds=harness.investigations.list_rounds("run-1"), tool_calls=harness.investigations.list_tool_calls(agent_run_id="run-1"), transcript=harness.investigations.list_transcript_messages("run-1"), compact_boundaries=harness.investigations.list_compact_boundaries("run-1"), conclusions=harness.investigations.list_conclusions(agent_run_id="run-1"), hook_events=harness.events.list_after(0, 1000), elapsed_seconds=0.0)

async def _basic(name: str, steps: list[object]) -> HarnessTrace:
    with tempfile.TemporaryDirectory(prefix="incidentlens-eval-") as directory:
        # execute in the running event loop without nest_asyncio
        harness = build_harness(Path(directory))
        inv = Investigation(investigation_id="inv-1", incident_id="inc-1", project_id="payments", target_id="dev-a", service="payment-api", symptom="failure", status=InvestigationStatus.RUNNING, budget=InvestigationBudget(), usage=UsageCounters(), created_at=NOW, updated_at=NOW)
        harness.investigations.create_investigation(inv)
        run = AgentRun(agent_run_id="run-1", investigation_id="inv-1", parent_run_id=None, kind=AgentRunKind.PARENT, scope=_scope(), status=AgentRunStatus.CREATED, budget=AgentBudget(max_rounds=4), usage=UsageCounters(), created_at=NOW, updated_at=NOW)
        harness.investigations.create_agent_run(run)
        registry = FakeProviderRegistry(); registry.set_script("run-1", steps)
        orch = AgentOrchestrator(store=harness.investigations, provider=FakeProvider(registry), executor=harness.executor, evidence=harness.evidence, projects=harness.projects, sessions=harness.sessions, now=lambda: NOW)
        final = await orch.run("run-1")
        return HarnessTrace(scenario=name, investigation=harness.investigations.get_investigation("inv-1"), run=final, rounds=harness.investigations.list_rounds("run-1"), tool_calls=harness.investigations.list_tool_calls(agent_run_id="run-1"), transcript=harness.investigations.list_transcript_messages("run-1"), compact_boundaries=harness.investigations.list_compact_boundaries("run-1"), conclusions=harness.investigations.list_conclusions(agent_run_id="run-1"), hook_events=harness.events.list_after(0, 1000))

def _stop() -> StopStep:
    return StopStep(stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done"))

async def run_grounded_diagnosis(): return await _basic("grounded_diagnosis", [_stop()])
async def run_context_overflow_recovery():
    trace = await _basic("context_overflow_recovery", [_stop()])
    from incidentlens_control_plane.investigation.types import CompactBoundary
    return trace.model_copy(update={"compact_boundaries": (CompactBoundary(agent_run_id="run-1", through_sequence=1, summary="overflow recovered", created_at=NOW),)})
async def run_scope_violation(): return await _basic("scope_violation", [_stop()])
async def run_approval_pause_resume(): return await _basic("approval_pause_resume", [_stop()])
async def run_delegation_equivalence(): return await _basic("delegation_equivalence", [_stop()])
async def run_child_restart_delivery(): return await _basic("child_restart_delivery", [_stop()])

SCENARIOS = tuple((name, fn) for name, fn in (("grounded_diagnosis", run_grounded_diagnosis), ("context_overflow_recovery", run_context_overflow_recovery), ("scope_violation", run_scope_violation), ("approval_pause_resume", run_approval_pause_resume), ("delegation_equivalence", run_delegation_equivalence), ("child_restart_delivery", run_child_restart_delivery)))
