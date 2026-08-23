"""Cross-investigation Project Memory end-to-end tests over the real loop.

These build a real runtime (orchestrator, executor, evidence, project memory)
with a scripted fake provider for the agent loop and a scripted fake model
transport for extraction/selection — no network is ever touched.  They prove:

1. A completed parent investigation automatically extracts verified memory
   backed by the evidence the run owns.
2. A fresh investigation receives a bounded, advisory Project Memory
   attachment in its very first parent request.
3. A single model batch that smuggles an unverified hypothesis, foreign
   evidence, a secret, or a raw log block persists only its valid facts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.investigation.fake_provider import (
    FakeProviderRegistry,
    RequestToolsStep,
    StopStep,
)
from incidentlens_control_plane.investigation.provider import (
    Conclusion,
    StopSignal,
    ToolRequest,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentScope,
    InvestigationBudget,
    StopReason,
    ToolResultBlock,
)
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.project_memory.types import (
    ProjectMemoryEntry,
    ProjectMemoryKind,
)
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
from incidentlens_control_plane.runtime import RuntimeServices, build_runtime

PROJECT_ID = "project-1"
TARGET_ID = "target-a"
SERVICE = "canary-db"
LOG_FILE = PurePosixPath("/workspace/service/live.log")


def _target() -> TargetRegistration:
    return TargetRegistration(target_id=TARGET_ID, host="127.0.0.1", ssh_user="incidentlens")


def _service() -> ServiceRegistration:
    return ServiceRegistration(
        compose_service=SERVICE,
        container_names=("canary-db",),
        allowed_log_paths=("/workspace/service/live.log",),
        allowed_host_paths=(PurePosixPath("/workspace/service"),),
        allowed_container_paths=(PurePosixPath("/workspace/service"),),
    )


def _settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        data_dir=tmp_path / "runtime",
        report_output_dir=tmp_path / "reports",
        max_active_investigations=4,
    )


def _agent_scope() -> AgentScope:
    return AgentScope(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        scope=LogScope.HOST,
        allowed_host_paths=(PurePosixPath("/workspace/service"),),
    )


def _run_budget() -> AgentBudget:
    return AgentBudget(max_rounds=6, max_tool_calls=6, max_no_new_evidence_rounds=2)


def _make_runtime(
    tmp_path: Path,
    *,
    transport: FakeTransportFactory,
    model_transport: _MemoryModelTransport | None = None,
) -> RuntimeServices:
    return build_runtime(
        _settings(tmp_path),
        transport_factory=transport,
        fake_provider_registry=_GroundingRegistry(),
        model_transport=model_transport or _MemoryModelTransport(),
    )


class _GroundingRegistry(FakeProviderRegistry):
    """Grounds a scripted completion on the evidence the run actually owns."""

    def pop_step(self, run_id: str, request: object) -> object:
        step = super().pop_step(run_id, request)
        if not isinstance(step, StopStep) or step.conclusion is None:
            return step
        evidence_ids = tuple(
            evidence_id
            for message in request.messages
            for block in message.blocks
            if isinstance(block, ToolResultBlock)
            for evidence_id in block.evidence_ids
        )
        if not evidence_ids:
            return step
        return step.model_copy(
            update={"conclusion": step.conclusion.model_copy(update={"evidence_ids": evidence_ids})}
        )


class _MemoryModelTransport:
    """A scripted model transport that dispatches extraction vs selection."""

    def __init__(
        self,
        *,
        extract_candidates=None,
        select_memory_ids: tuple[str, ...] = (),
        select_garbage: bool = False,
    ) -> None:
        self._extract_candidates = extract_candidates
        self._select_memory_ids = select_memory_ids
        self._select_garbage = select_garbage
        self.calls: list[dict[str, object]] = []

    def chat_completions(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(payload)
        system = payload["messages"][0]["content"]
        if "project memory extractor" in system:
            owned = self._owned_ids(payload)
            if self._extract_candidates is not None:
                candidates = self._extract_candidates(owned)
            else:
                candidates = [
                    {
                        "memory_id": "mem-auto-1",
                        "kind": "verified_fact",
                        "fact": "canary-db recovered by restarting the canary pod",
                        "service_names": [SERVICE],
                        "evidence_ids": list(owned[:1]),
                    }
                ]
            return {
                "choices": [{"message": {"content": json.dumps({"candidates": candidates})}}]
            }
        if "project memory selector" in system:
            if self._select_garbage:
                return {"choices": [{"message": {"content": "not-json"}}]}
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"memory_ids": list(self._select_memory_ids)}
                            )
                        }
                    }
                ]
            }
        raise AssertionError(f"unexpected project memory prompt: {system[:40]}")

    @staticmethod
    def _owned_ids(payload: dict[str, object]) -> list[str]:
        user = json.loads(payload["messages"][1]["content"])  # type: ignore[arg-type]
        return list(user.get("owned_evidence_ids", ()))


async def _seed_project(runtime: RuntimeServices, factory: FakeTransportFactory) -> None:
    target = _target()
    transport = await factory.connect(target)
    await transport.write_bytes(LOG_FILE, b"ERROR checkout timeout\n")
    runtime.projects.create(
        ProjectRegistration(
            project_id=PROJECT_ID,
            display_name="Canary",
            targets=(target,),
            services=(_service(),),
        ),
        now=datetime.now(UTC),
    )


def _new_investigation(runtime: RuntimeServices, *, symptom: str) -> object:
    return runtime.investigations.create_investigation(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        service=SERVICE,
        symptom=symptom,
        incident_id="incident-1",
        budget=InvestigationBudget(
            max_rounds=6, max_tool_calls=6, max_no_new_evidence_rounds=2
        ),
    )


async def complete_with_conclusion(
    runtime: RuntimeServices, factory: FakeTransportFactory
) -> tuple[str, ...]:
    """Run a parent to a grounded completion and return its owned evidence ids."""
    await _seed_project(runtime, factory)
    investigation = _new_investigation(
        runtime, symptom="canary database errors after the checkout rollout"
    )
    runtime.fake_provider.set_pending_script(
        [
            RequestToolsStep(
                tool_requests=(
                    ToolRequest(
                        tool_call_id="log-1",
                        tool_name="log_query",
                        arguments={
                            "service_name": SERVICE,
                            "source_kind": "file",
                            "source_ref": "/workspace/service/live.log",
                        },
                    ),
                ),
            ),
            StopStep(
                stop_signal=StopSignal(
                    stop_reason=StopReason.COMPLETED,
                    summary="canary recovered after restart",
                ),
                conclusion=Conclusion(summary="canary-db recovered after restart"),
            ),
        ]
    )
    run = await runtime.investigations.start(
        getattr(investigation, "investigation_id"),
        _agent_scope(),
        parent_budget=_run_budget(),
    )
    if run.status.value == "paused_missing_evidence":
        run = await runtime.investigations.resume_run(run.agent_run_id)
    assert run.status.value == "completed"
    owned = tuple(reference.evidence_id for reference in run.evidence)
    assert owned, "completed run must own evidence"
    return owned


def seed_verified_project_memory(runtime: RuntimeServices) -> None:
    moment = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    runtime.project_memory_store.upsert(
        ProjectMemoryEntry(
            memory_id="mem-past",
            project_id=PROJECT_ID,
            service_names=(SERVICE,),
            fact="canary-db fails over to pending replicas when the disk fills",
            kind=ProjectMemoryKind.FAILURE_MODE,
            source_investigation_id="inv-past",
            evidence_ids=("ev-past",),
            created_at=moment,
            last_confirmed_at=moment,
        )
    )


async def build_first_parent_request(
    runtime: RuntimeServices, *, symptom: str
) -> object:
    investigation = _new_investigation(runtime, symptom=symptom)
    runtime.fake_provider.set_pending_script(
        [
            StopStep(
                stop_signal=StopSignal(
                    stop_reason=StopReason.COMPLETED,
                    summary="first request only",
                ),
                conclusion=None,
            )
        ]
    )
    run = await runtime.investigations.start(
        getattr(investigation, "investigation_id"),
        _agent_scope(),
        parent_budget=_run_budget(),
    )
    requests = runtime.fake_provider.requests(run.agent_run_id)
    assert requests, "first parent request was never built"
    return requests[0]


@pytest.mark.asyncio
async def test_completed_parent_extracts_verified_memory(tmp_path: Path) -> None:
    factory = FakeTransportFactory()
    runtime = _make_runtime(tmp_path, transport=factory)
    try:
        owned = await complete_with_conclusion(runtime, factory)
        await runtime.project_memory.drain_pending()

        entries = runtime.project_memory_store.list_active(PROJECT_ID, limit=5)
        assert entries
        assert entries[0].evidence_ids
        assert set(entries[0].evidence_ids) <= set(owned)
    finally:
        await runtime.recovery.shutdown()
        await runtime.sessions.close_all()


@pytest.mark.asyncio
async def test_fresh_investigation_receives_relevant_memory_as_advisory(tmp_path: Path) -> None:
    factory = FakeTransportFactory()
    runtime = _make_runtime(tmp_path, transport=factory)
    try:
        await _seed_project(runtime, factory)
        seed_verified_project_memory(runtime)

        request = await build_first_parent_request(
            runtime, symptom="canary database errors"
        )
        header = request.messages[0].blocks[0].text
        assert "Project memory (advisory; revalidate" in header
        assert "source investigation" in header
        assert "inv-past" in header
    finally:
        await runtime.recovery.shutdown()
        await runtime.sessions.close_all()


@pytest.mark.asyncio
async def test_hypotheses_and_secrets_never_persist_but_valid_fact_does(
    tmp_path: Path,
) -> None:
    factory = FakeTransportFactory()

    def bad_batch(owned: list[str]) -> list[dict[str, object]]:
        valid_evidence = list(owned[:1])
        return [
            {
                "memory_id": "mem-hyp",
                "kind": "unverified_hypothesis",
                "fact": "the outage may be an upstream capacity limit",
                "service_names": [SERVICE],
                "evidence_ids": valid_evidence,
            },
            {
                "memory_id": "mem-foreign",
                "kind": "verified_fact",
                "fact": "auth-api depends on canary-db",
                "service_names": [SERVICE],
                "evidence_ids": ["ev-foreign"],
            },
            {
                "memory_id": "mem-secret",
                "kind": "verified_fact",
                "fact": "the api key = sk-abcd1234efgh5678 for the service",
                "service_names": [SERVICE],
                "evidence_ids": valid_evidence,
            },
            {
                "memory_id": "mem-raw",
                "kind": "raw_log",
                "fact": "2026-08-14T10:00:01Z ERROR checkout request_id=req-42 timeout",
                "service_names": [SERVICE],
                "evidence_ids": valid_evidence,
            },
            {
                "memory_id": "mem-good",
                "kind": "failure_mode",
                "fact": "canary-db fails over when the disk fills",
                "service_names": [SERVICE],
                "evidence_ids": valid_evidence,
            },
        ]

    runtime = _make_runtime(
        tmp_path,
        transport=factory,
        model_transport=_MemoryModelTransport(extract_candidates=bad_batch),
    )
    try:
        await complete_with_conclusion(runtime, factory)
        await runtime.project_memory.drain_pending()

        active = runtime.project_memory_store.list_active(PROJECT_ID, limit=10)
        assert [item.memory_id for item in active] == ["mem-good"]
        assert all(
            item.kind is not ProjectMemoryKind.UNVERIFIED_HYPOTHESIS for item in active
        )
        assert all(
            "api key" not in item.fact and "sk-" not in item.fact for item in active
        )
        assert all("request_id" not in item.fact for item in active)
    finally:
        await runtime.recovery.shutdown()
        await runtime.sessions.close_all()
