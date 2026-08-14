"""Tests for the provider-neutral model contract and the scripted Fake Provider.

The Fake Provider never executes tools or writes stores; these tests route its
output through the real ``ProviderOutputValidator`` (which reuses the real
``InvestigationGuard``) and ground evidence in a real ``EvidenceStore``, so the
scripted simulations exercise the same guard/tool/evidence/approval paths the
orchestrator will use at runtime.
"""

from __future__ import annotations

import hashlib
import inspect
import sqlite3
from datetime import UTC, datetime

import pytest
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.evidence.types import EvidenceKind, EvidenceRef
from incidentlens_control_plane.investigation.fake_provider import (
    CrashStep,
    DelegateChildStep,
    ErrorStep,
    FakeProvider,
    FakeProviderRegistry,
    MalformedStep,
    RequestToolsStep,
    SchemaViolationStep,
    ScriptExhausted,
    ScriptNotFound,
    StopStep,
)
from incidentlens_control_plane.investigation.provider import (
    AgentTurnRequest,
    AgentTurnResult,
    ChildDelegationRequest,
    HypothesisProposal,
    InvestigationSnapshot,
    ModelProvider,
    ProviderContextMismatch,
    ProviderCrash,
    ProviderError,
    ProviderOutputRejected,
    ProviderOutputValidator,
    ProviderValidation,
    RunCheckpoint,
    StopSignal,
    ToolRequest,
    ToolSchema,
)
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
    EvidenceReference,
    Investigation,
    InvestigationBudget,
    ProviderUsage,
    StopReason,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope
from pydantic import ValidationError

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)


def make_investigation(**kwargs: object) -> Investigation:
    fields: dict[str, object] = {
        "investigation_id": "inv-1",
        "incident_id": "inc-123",
        "project_id": "proj-1",
        "target_id": "prod-a",
        "service": "orders",
        "symptom": "checkout requests are failing",
        "status": InvestigationStatus.RUNNING,
        "budget": InvestigationBudget(),
        "usage": UsageCounters(),
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(kwargs)
    return Investigation(**fields)


def make_run(
    *,
    agent_run_id: str = "run-1",
    investigation_id: str = "inv-1",
    kind: AgentRunKind = AgentRunKind.PARENT,
    parent_run_id: str | None = None,
    scope: AgentScope | None = None,
    status: AgentRunStatus = AgentRunStatus.RUNNING,
    **kwargs: object,
) -> AgentRun:
    fields: dict[str, object] = {
        "agent_run_id": agent_run_id,
        "investigation_id": investigation_id,
        "kind": kind,
        "parent_run_id": parent_run_id,
        "scope": scope
        or AgentScope(
            project_id="proj-1", target_id="prod-a", scope=LogScope.HOST
        ),
        "status": status,
        "budget": AgentBudget(),
        "usage": UsageCounters(),
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(kwargs)
    return AgentRun(**fields)


def make_container_run(**kwargs: object) -> AgentRun:
    return make_run(
        agent_run_id="run-1",
        scope=AgentScope(
            project_id="proj-1",
            target_id="prod-a",
            scope=LogScope.CONTAINER,
            service_name="orders",
            container_name="orders-1",
        ),
        **kwargs,
    )


def make_evidence_ref(
    evidence_ref_id: str = "ev-1", *, agent_run_id: str = "run-1"
) -> EvidenceRef:
    content = f"redacted evidence content for {evidence_ref_id}"
    return EvidenceRef(
        evidence_ref_id=evidence_ref_id,
        incident_id="inc-123",
        evidence_kind=EvidenceKind.COMMAND_OUTPUT,
        agent_run_id=agent_run_id,
        project_id="proj-1",
        target_id="prod-a",
        service_name="orders",
        source_ref="/var/log/orders",
        content_redacted=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        redaction_summary={"redacted": 0},
        created_at=NOW,
        created_by="test",
    )


def reference_from_ref(ref: EvidenceRef) -> EvidenceReference:
    return EvidenceReference(
        evidence_id=ref.evidence_ref_id,
        operation_id="op-1",
        summary=ref.content_redacted,
    )


def default_tool_schemas() -> tuple[ToolSchema, ...]:
    return (
        ToolSchema(
            tool_name="logs.query",
            description="Search bounded log records for a service.",
            parameters_json_schema={
                "type": "object",
                "required": ["query"],
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500},
                    "service_name": {"type": "string", "minLength": 1, "maxLength": 120},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "follow": {"type": "boolean"},
                },
            },
        ),
        ToolSchema(
            tool_name="shell.run",
            description="Run a bounded, read-only command on the target.",
            parameters_json_schema={
                "type": "object",
                "required": ["command"],
                "additionalProperties": False,
                "properties": {
                    "command": {"type": "string", "minLength": 1, "maxLength": 500},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
                },
            },
        ),
        ToolSchema(
            tool_name="files.read",
            description="Read a bounded file snapshot.",
            parameters_json_schema={
                "type": "object",
                "required": ["path"],
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 500, "pattern": "^/"},
                    "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1_048_576},
                },
            },
        ),
        ToolSchema(
            tool_name="container.inspect",
            description="Inspect a container's runtime state.",
            allowed_scope=LogScope.CONTAINER,
            parameters_json_schema={
                "type": "object",
                "required": ["container_name"],
                "additionalProperties": False,
                "properties": {
                    "container_name": {"type": "string", "minLength": 1, "maxLength": 120},
                    "namespace": {"type": "string", "minLength": 1, "maxLength": 120},
                },
            },
        ),
        ToolSchema(
            tool_name="registry.discover",
            description="Propose widening registry scope; requires approval.",
            requires_approval=True,
            parameters_json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "namespace": {"type": "string", "minLength": 1, "maxLength": 120},
                },
            },
        ),
    )


def _checkpoint(run: AgentRun) -> RunCheckpoint:
    return RunCheckpoint(
        agent_run_id=run.agent_run_id,
        kind=run.kind,
        status=run.status,
        round_number=run.usage.rounds + 1,
        parent_run_id=run.parent_run_id,
        scope=run.scope,
        budget=run.budget,
        usage=run.usage,
    )


def _snapshot(investigation: Investigation) -> InvestigationSnapshot:
    return InvestigationSnapshot(
        investigation_id=investigation.investigation_id,
        incident_id=investigation.incident_id,
        service=investigation.service,
        allowed_log_paths=(),
        symptom=investigation.symptom,
        status=investigation.status,
        budget=investigation.budget,
        usage=investigation.usage,
    )


def make_request(
    run: AgentRun,
    investigation: Investigation,
    *,
    evidence: tuple[EvidenceReference, ...] = (),
    hypotheses: tuple = (),
    child_reports: tuple = (),
    tool_schemas: tuple[ToolSchema, ...] | None = None,
) -> AgentTurnRequest:
    return AgentTurnRequest(
        checkpoint=_checkpoint(run),
        investigation=_snapshot(investigation),
        hypotheses=hypotheses,
        evidence=evidence,
        child_reports=child_reports,
        tool_schemas=tool_schemas or default_tool_schemas(),
    )


def validate(
    request: AgentTurnRequest, run: AgentRun, result: AgentTurnResult
) -> ProviderValidation:
    return ProviderOutputValidator(request, run).validate(result)


# -- provider contract surface ------------------------------------------------


def test_provider_interface_accepts_request_and_returns_result():
    assert issubclass(FakeProvider, ModelProvider)
    assert inspect.isabstract(ModelProvider)
    with pytest.raises(TypeError):
        ModelProvider()  # type: ignore[abstract]
    parameters = list(inspect.signature(ModelProvider.generate_turn).parameters)
    assert parameters == ["self", "request"]


def test_request_context_has_no_hidden_reasoning_field():
    request = make_request(make_run(), make_investigation())
    with pytest.raises(ValidationError):
        AgentTurnRequest.model_validate(
            {**request.model_dump(), "hidden_reasoning": "draft chain of thought"}
        )


def test_request_rejects_duplicate_evidence_ids():
    run = make_run()
    ref = EvidenceReference(evidence_id="ev-1", operation_id="op-1", summary="dup")
    with pytest.raises(ValidationError, match="unique by evidence_id"):
        AgentTurnRequest(
            checkpoint=_checkpoint(run),
            investigation=_snapshot(make_investigation()),
            evidence=(ref, ref),
        )


def test_tool_request_rejects_non_json_arguments():
    with pytest.raises(ValidationError, match="JSON-compatible"):
        ToolRequest(
            tool_call_id="t1",
            tool_name="logs.query",
            arguments={"query": object()},  # type: ignore[dict-item]
        )


def test_turn_rejects_tools_plus_stop_signal():
    with pytest.raises(ValidationError, match="not more than one"):
        AgentTurnResult(
            tool_requests=(
                ToolRequest(tool_call_id="t1", tool_name="logs.query", arguments={"query": "x"}),
            ),
            stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done"),
        )


def test_turn_rejects_delegation_plus_stop_signal():
    with pytest.raises(ValidationError, match="not more than one"):
        AgentTurnResult(
            child_delegation=ChildDelegationRequest(
                child_run_id="child-1",
                task_prompt="investigate",
                scope=AgentScope(project_id="proj-1", target_id="prod-a", scope=LogScope.HOST),
            ),
            stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done"),
        )


def test_turn_rejects_delegation_plus_conclusion():
    with pytest.raises(ValidationError, match="must not declare conclusions"):
        AgentTurnResult(
            child_delegation=ChildDelegationRequest(
                child_run_id="child-1",
                task_prompt="investigate",
                scope=AgentScope(project_id="proj-1", target_id="prod-a", scope=LogScope.HOST),
            ),
            conclusions=(Conclusion(summary="root caused"),),
        )


# -- fake provider: scripted behavior ----------------------------------------


async def test_fake_replays_request_tools_step():
    registry = FakeProviderRegistry()
    provider = FakeProvider(registry)
    request = make_request(make_run(), make_investigation())
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    ToolRequest(
                        tool_call_id="t1",
                        tool_name="logs.query",
                        arguments={"query": "timeout", "limit": 50},
                    ),
                ),
                usage=ProviderUsage(input_tokens=100, output_tokens=10, output_bytes=64),
            )
        ],
    )

    result = await provider.generate_turn(request)

    assert result.tool_requests[0].tool_name == "logs.query"
    assert result.tool_requests[0].arguments == {"query": "timeout", "limit": 50}
    assert result.usage == ProviderUsage(input_tokens=100, output_tokens=10, output_bytes=64)
    assert result.stop_signal is None
    assert registry.remaining("run-1") == 0


async def test_fake_replays_stop_step():
    registry = FakeProviderRegistry()
    provider = FakeProvider(registry)
    registry.set_script(
        "run-1",
        [
            StopStep(
                stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="root caused"),
                conclusion=Conclusion(
                    summary="db pool exhaustion", evidence_ids=("ev-1",)
                ),
            )
        ],
    )

    result = await provider.generate_turn(make_request(make_run(), make_investigation()))

    assert result.stop_signal is not None
    assert result.stop_signal.stop_reason is StopReason.COMPLETED
    assert result.conclusions[0].summary == "db pool exhaustion"


async def test_fake_replays_delegate_child_step():
    registry = FakeProviderRegistry()
    provider = FakeProvider(registry)
    registry.set_script(
        "run-1",
        [
            DelegateChildStep(
                delegation=ChildDelegationRequest(
                    child_run_id="child-1",
                    task_prompt="inspect the payments container",
                    scope=AgentScope(
                        project_id="proj-1",
                        target_id="prod-a",
                        scope=LogScope.CONTAINER,
                        service_name="orders",
                        container_name="orders-1",
                    ),
                )
            )
        ],
    )

    result = await provider.generate_turn(make_request(make_run(), make_investigation()))

    assert result.child_delegation is not None
    assert result.child_delegation.child_run_id == "child-1"


async def test_fake_error_step_raises_provider_error():
    registry = FakeProviderRegistry()
    provider = FakeProvider(registry)
    registry.set_script("run-1", [ErrorStep(message="rate limited", retryable=True)])

    with pytest.raises(ProviderError) as excinfo:
        await provider.generate_turn(make_request(make_run(), make_investigation()))

    assert excinfo.value.retryable is True


async def test_fake_crash_step_raises_provider_crash():
    registry = FakeProviderRegistry()
    provider = FakeProvider(registry)
    registry.set_script("run-1", [CrashStep(message="segfault")])

    with pytest.raises(ProviderCrash):
        await provider.generate_turn(make_request(make_run(), make_investigation()))


async def test_fake_exhausted_script_raises():
    registry = FakeProviderRegistry()
    provider = FakeProvider(registry)
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    ToolRequest(
                        tool_call_id="t1",
                        tool_name="logs.query",
                        arguments={"query": "x"},
                    ),
                )
            )
        ],
    )
    request = make_request(make_run(), make_investigation())

    await provider.generate_turn(request)
    with pytest.raises(ScriptExhausted):
        await provider.generate_turn(request)


async def test_fake_schema_violation_step_raises_validation_error():
    registry = FakeProviderRegistry()
    provider = FakeProvider(registry)
    registry.set_script("run-1", [SchemaViolationStep(raw_payload={"tool_requests": "not-a-list"})])

    with pytest.raises(ValidationError):
        await provider.generate_turn(make_request(make_run(), make_investigation()))


def test_registry_scripts_are_per_run_and_persistent():
    registry = FakeProviderRegistry()
    registry.set_script(
        "run-1",
        [
            RequestToolsStep(
                tool_requests=(
                    ToolRequest(
                        tool_call_id="t1",
                        tool_name="logs.query",
                        arguments={"query": "a"},
                    ),
                )
            ),
            RequestToolsStep(
                tool_requests=(
                    ToolRequest(
                        tool_call_id="t2",
                        tool_name="files.read",
                        arguments={"path": "/etc/hosts"},
                    ),
                )
            ),
        ],
    )
    registry.set_script(
        "run-2",
        [
            StopStep(
                stop_signal=StopSignal(
                    stop_reason=StopReason.UNCERTAIN_STATE,
                    summary="blurry",
                )
            )
        ],
    )

    assert registry.has_script("run-1")
    assert registry.remaining("run-1") == 2
    assert registry.remaining("run-2") == 1
    assert registry.remaining("run-99") == 0

    first = registry.pop("run-1")
    assert first.kind == "request_tools"
    assert registry.remaining("run-1") == 1
    assert registry.remaining("run-2") == 1  # other runs are untouched


def test_registry_steps_are_addressable_by_index():
    registry = FakeProviderRegistry()
    first = RequestToolsStep(
        tool_requests=(
            ToolRequest(
                tool_call_id="t1",
                tool_name="logs.query",
                arguments={"query": "a"},
            ),
        )
    )
    second = RequestToolsStep(
        tool_requests=(
            ToolRequest(
                tool_call_id="t2",
                tool_name="files.read",
                arguments={"path": "/etc"},
            ),
        )
    )
    registry.set_script("run-1", [first, second])

    assert registry.peek("run-1", 0) is first
    assert registry.peek("run-1", 1) is second
    assert registry.remaining("run-1") == 2  # peek does not consume
    with pytest.raises(ScriptNotFound):
        registry.peek("run-1", 2)


# -- validator: request/run identity consistency ------------------------------


def test_validator_rejects_run_identity_mismatch():
    request = make_request(make_run(), make_investigation())
    other_run = make_run(agent_run_id="run-2")

    with pytest.raises(ProviderContextMismatch, match="but the provided run is"):
        ProviderOutputValidator(request, other_run)


def test_validator_rejects_investigation_identity_mismatch():
    request = make_request(make_run(), make_investigation())
    other_run = make_run(investigation_id="inv-2")

    with pytest.raises(ProviderContextMismatch, match="run belongs to"):
        ProviderOutputValidator(request, other_run)


def test_validator_rejects_scope_mismatch():
    request = make_request(make_run(), make_investigation())
    other_run = make_container_run()  # same run id, different scope

    with pytest.raises(ProviderContextMismatch, match="scope does not match"):
        ProviderOutputValidator(request, other_run)


def test_validator_accepts_matching_run_and_request():
    run = make_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        tool_requests=(
            ToolRequest(tool_call_id="t1", tool_name="logs.query", arguments={"query": "x"}),
        )
    )

    assert validate(request, run, result).requires_approval is False


# -- validator: tool allowlist, arguments and scope ---------------------------


def test_validator_rejects_unallowlisted_tool():
    run = make_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        tool_requests=(ToolRequest(tool_call_id="t1", tool_name="db.exec"),)
    )

    with pytest.raises(ProviderOutputRejected, match="not allowlisted"):
        validate(request, run, result)


def test_validator_rejects_tool_with_missing_required_argument():
    run = make_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        tool_requests=(
            ToolRequest(tool_call_id="t1", tool_name="logs.query", arguments={"limit": 200}),
        )
    )

    with pytest.raises(ProviderOutputRejected, match="missing required property"):
        validate(request, run, result)


def test_validator_rejects_tool_argument_of_wrong_type():
    run = make_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        tool_requests=(
            ToolRequest(
                tool_call_id="t1",
                tool_name="logs.query",
                arguments={"query": "timeout", "limit": "many"},
            ),
        )
    )

    with pytest.raises(ProviderOutputRejected, match="must be an integer"):
        validate(request, run, result)


def test_validator_rejects_unknown_tool_argument_when_disallowed():
    run = make_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        tool_requests=(
            ToolRequest(
                tool_call_id="t1",
                tool_name="logs.query",
                arguments={"query": "timeout", "limit": 50, "sneaky": True},
            ),
        )
    )

    with pytest.raises(ProviderOutputRejected, match="unknown properties"):
        validate(request, run, result)


def test_validator_rejects_duplicate_tool_call_ids():
    run = make_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        tool_requests=(
            ToolRequest(tool_call_id="t1", tool_name="logs.query", arguments={"query": "a"}),
            ToolRequest(
                tool_call_id="t1",
                tool_name="files.read",
                arguments={"path": "/etc/hosts"},
            ),
        )
    )

    with pytest.raises(ProviderOutputRejected, match="duplicate tool_call_id"):
        validate(request, run, result)


def test_validator_rejects_container_only_tool_on_host_run():
    run = make_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        tool_requests=(
            ToolRequest(
                tool_call_id="t1",
                tool_name="container.inspect",
                arguments={"container_name": "orders-1"},
            ),
        )
    )

    with pytest.raises(ProviderOutputRejected, match="requires container scope"):
        validate(request, run, result)


def test_validator_accepts_container_only_tool_on_container_run():
    run = make_container_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        tool_requests=(
            ToolRequest(
                tool_call_id="t1",
                tool_name="container.inspect",
                arguments={"container_name": "orders-1"},
            ),
        )
    )

    assert validate(request, run, result).requires_approval is False


# -- validator: evidence ownership (real guard + real evidence store) ---------


@pytest.fixture
def evidence_store(tmp_path) -> EvidenceStore:
    store = EvidenceStore(lambda: sqlite3.connect(tmp_path / "evidence.db"))
    store.migrate()
    return store


def test_validator_accepts_hypothesis_citing_owned_evidence(evidence_store):
    stored = evidence_store.create(make_evidence_ref("ev-1"))
    run = make_run(evidence=(reference_from_ref(stored),))
    request = make_request(run, make_investigation(), evidence=(reference_from_ref(stored),))
    result = AgentTurnResult(
        hypotheses=(
            HypothesisProposal(summary="db pool exhausted", evidence_ids=("ev-1",)),
        )
    )

    assert validate(request, run, result).requires_approval is False


def test_validator_rejects_hypothesis_citing_unowned_evidence(evidence_store):
    evidence_store.create(make_evidence_ref("ev-1"))
    run = make_run(evidence=())
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        hypotheses=(HypothesisProposal(summary="pool exhausted", evidence_ids=("ev-1",)),)
    )

    with pytest.raises(ProviderOutputRejected, match="not collected in this investigation"):
        validate(request, run, result)


def test_validator_accepts_hypothesis_before_it_has_evidence(evidence_store):
    run = make_run(evidence=())
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        hypotheses=(HypothesisProposal(summary="maybe the pool is exhausted"),)
    )

    assert validate(request, run, result).requires_approval is False


def test_validator_accepts_conclusion_grounded_in_run_evidence(evidence_store):
    stored = evidence_store.create(make_evidence_ref("ev-1"))
    run = make_run(evidence=(reference_from_ref(stored),))
    request = make_request(run, make_investigation(), evidence=(reference_from_ref(stored),))
    result = AgentTurnResult(
        conclusions=(Conclusion(summary="pool exhaustion", evidence_ids=("ev-1",)),)
    )

    assert validate(request, run, result).requires_approval is False


def test_validator_rejects_conclusion_citing_fabricated_evidence(evidence_store):
    run = make_run(evidence=())
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        conclusions=(Conclusion(summary="pool exhaustion", evidence_ids=("ev-404",)),)
    )

    with pytest.raises(ProviderOutputRejected, match="not collected in this investigation"):
        validate(request, run, result)


# -- validator: stop signals --------------------------------------------------


def test_validator_rejects_non_declarable_stop_reason():
    run = make_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        stop_signal=StopSignal(stop_reason=StopReason.BUDGET_ROUNDS, summary="budget hit")
    )

    with pytest.raises(ProviderOutputRejected, match="not declarable"):
        validate(request, run, result)


def test_validator_accepts_declarable_stop_reason():
    run = make_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        stop_signal=StopSignal(stop_reason=StopReason.COMPLETED, summary="done")
    )

    assert validate(request, run, result).requires_approval is False


def test_validator_rejects_empty_turn():
    run = make_run()
    request = make_request(run, make_investigation())

    with pytest.raises(ProviderOutputRejected, match="empty turn"):
        validate(request, run, AgentTurnResult())


# -- validator: child delegation scope and ownership --------------------------


def test_validator_accepts_host_child_within_host_run():
    run = make_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        child_delegation=ChildDelegationRequest(
            child_run_id="child-1",
            task_prompt="investigate the checkout path",
            scope=AgentScope(project_id="proj-1", target_id="prod-a", scope=LogScope.HOST),
        )
    )

    assert validate(request, run, result).requires_approval is False


def test_validator_accepts_container_child_narrowed_from_host_run():
    run = make_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        child_delegation=ChildDelegationRequest(
            child_run_id="child-1",
            task_prompt="inspect the orders container",
            scope=AgentScope(
                project_id="proj-1",
                target_id="prod-a",
                scope=LogScope.CONTAINER,
                service_name="orders",
                container_name="orders-1",
            ),
        )
    )

    assert validate(request, run, result).requires_approval is False


def test_validator_rejects_child_delegation_out_of_scope():
    run = make_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        child_delegation=ChildDelegationRequest(
            child_run_id="child-1",
            task_prompt="poke another target",
            scope=AgentScope(project_id="proj-1", target_id="prod-b", scope=LogScope.HOST),
        )
    )

    with pytest.raises(ProviderOutputRejected, match="must match the run"):
        validate(request, run, result)


def test_validator_rejects_child_with_same_run_id():
    run = make_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        child_delegation=ChildDelegationRequest(
            child_run_id="run-1",
            task_prompt="delegate to myself",
            scope=AgentScope(project_id="proj-1", target_id="prod-a", scope=LogScope.HOST),
        )
    )

    with pytest.raises(ProviderOutputRejected, match="must differ"):
        validate(request, run, result)


def test_validator_rejects_child_delegation_citing_unowned_evidence():
    run = make_run(evidence=())
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        child_delegation=ChildDelegationRequest(
            child_run_id="child-1",
            task_prompt="seed with evidence",
            scope=AgentScope(project_id="proj-1", target_id="prod-a", scope=LogScope.HOST),
            evidence_ids=("ev-7",),
        )
    )

    with pytest.raises(ProviderOutputRejected, match="cites evidence not collected"):
        validate(request, run, result)


# -- validator: budgets and approval ------------------------------------------


def test_validator_rejects_turn_exceeding_output_budget():
    run = make_run()
    request = make_request(run, make_investigation())
    over = AgentBudget().max_output_bytes_per_tool + 1
    result = AgentTurnResult(
        tool_requests=(
            ToolRequest(
                tool_call_id="t1",
                tool_name="logs.query",
                arguments={"query": "x"},
            ),
        ),
        usage=ProviderUsage(output_tokens=10, output_bytes=over),
    )

    with pytest.raises(ProviderOutputRejected, match="output budget"):
        validate(request, run, result)


def test_validator_flags_requires_approval_for_approval_tool():
    run = make_run()
    request = make_request(run, make_investigation())
    result = AgentTurnResult(
        tool_requests=(
            ToolRequest(
                tool_call_id="t1",
                tool_name="registry.discover",
                arguments={"namespace": "default"},
            ),
        )
    )

    outcome = validate(request, run, result)
    assert outcome.requires_approval is True


def test_validator_accepts_clean_turn_with_owned_evidence(evidence_store):
    stored = evidence_store.create(make_evidence_ref("ev-1"))
    run = make_run(evidence=(reference_from_ref(stored),))
    request = make_request(run, make_investigation(), evidence=(reference_from_ref(stored),))
    result = AgentTurnResult(
        tool_requests=(
            ToolRequest(
                tool_call_id="t1",
                tool_name="logs.query",
                arguments={"query": "timeout", "limit": 50},
            ),
        ),
        hypotheses=(
            HypothesisProposal(summary="db pool exhausted", evidence_ids=("ev-1",)),
        ),
        usage=ProviderUsage(input_tokens=120, output_tokens=30, output_bytes=512),
    )

    outcome = validate(request, run, result)
    assert outcome.requires_approval is False


# -- fake provider + validator end-to-end (malformed output) ------------------


async def test_malformed_step_output_is_rejected_by_real_validator():
    registry = FakeProviderRegistry()
    provider = FakeProvider(registry)
    run = make_run()
    request = make_request(run, make_investigation())
    registry.set_script(
        "run-1",
        [
            MalformedStep(
                result=AgentTurnResult(
                    tool_requests=(
                        ToolRequest(tool_call_id="t1", tool_name="not.a.real.tool"),
                    )
                )
            )
        ],
    )

    result = await provider.generate_turn(request)

    with pytest.raises(ProviderOutputRejected, match="not allowlisted"):
        validate(request, run, result)
