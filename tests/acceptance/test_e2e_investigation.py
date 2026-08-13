"""端到端验收测试（离线，使用 FakeProvider）。

覆盖 Phase 5 MVP 验收标准中的核心链路：注册项目 → 创建调查 → 启动
（真实编排器 + 脚本化 FakeProvider）→ 生成报告，以及审批流程与空调查报告。

不使用 Docker / 真实 SSH：所有远程工具调用都通过 FakeProvider 脚本驱动，
走真实的 ProviderOutputValidator、InvestigationGuard、state machine 与
evidence/approval 服务。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.investigation.fake_provider import (
    RequestToolsStep,
    StopStep,
)
from incidentlens_control_plane.investigation.provider import (
    Conclusion,
    StopSignal,
    ToolRequest,
)
from incidentlens_control_plane.investigation.state_machine import AgentRunStatus
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    EvidenceReference,
    StopReason,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.reports.types import ReportBundle
from incidentlens_control_plane.runtime import build_runtime

PROJECT_ID = "proj-test"
TARGET_ID = "target-test"
SERVICE = "web"
CONTAINER = "web-1"


@pytest.fixture()
def runtime(tmp_path):
    settings = RuntimeSettings(data_dir=tmp_path)
    return build_runtime(settings)


def _register_project(
    runtime,
    *,
    project_id: str = PROJECT_ID,
    target_id: str = TARGET_ID,
    service: str = SERVICE,
) -> object:
    """注册一个测试项目（MVP #1: 项目注册）。"""
    registration = ProjectRegistration(
        project_id=project_id,
        display_name="Test Project",
        targets=(
            TargetRegistration(
                target_id=target_id,
                host="localhost",
                ssh_user="test",
            ),
        ),
        services=(
            ServiceRegistration(
                compose_service=service,
                container_names=(CONTAINER,),
            ),
        ),
    )
    return runtime.projects.create(registration, now=datetime.now(UTC))


def _make_parent_run(runtime, investigation, *, agent_run_id: str, scope: AgentScope) -> AgentRun:
    """创建一个已知 id 的父运行，让测试可以预先编排 FakeProvider 脚本。

    ``InvestigationService.start`` 会发现这个已存在且非终态的父运行并
    通过 ``orchestrator.run`` 恢复它（这正是 ``start`` 的 resume 分支）。
    """
    now = datetime.now(UTC)
    run = AgentRun(
        agent_run_id=agent_run_id,
        investigation_id=investigation.investigation_id,
        parent_run_id=None,
        kind=AgentRunKind.PARENT,
        scope=scope,
        status=AgentRunStatus.CREATED,
        budget=AgentBudget(),
        usage=UsageCounters(),
        created_at=now,
        updated_at=now,
    )
    runtime.investigation_store.create_agent_run(run)
    return run


def _seed_run_evidence(runtime, run: AgentRun, investigation) -> str:
    """记录一条运行拥有的验证证据，并把引用挂到运行上。

    完成性 stop 需要至少一条“结论引用了运行实际拥有的证据”，这里通过
    EvidenceService 的真实路径写入并绑定，走与产品一致的证据管线。
    """
    ref = runtime.evidence_service.record_validation_result(
        agent_run_id=run.agent_run_id,
        incident_id=investigation.incident_id,
        project_id=investigation.project_id,
        target_id=investigation.target_id,
        service_name=investigation.service,
        source_ref="e2e:seed",
        validator="e2e-test",
        passed=True,
        detail="seed validation evidence for the offline E2E run",
        created_by="test",
        now=datetime.now(UTC),
    )
    run = run.model_copy(
        update={
            "evidence": (
                EvidenceReference(
                    evidence_id=ref.evidence_ref_id,
                    operation_id="e2e:seed",
                    summary="seed evidence",
                ),
            )
        }
    )
    runtime.investigation_store.update_agent_run(run)
    return ref.evidence_ref_id


def _host_scope() -> AgentScope:
    return AgentScope(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        scope=LogScope.HOST,
    )


async def test_full_investigation_lifecycle(runtime) -> None:
    """MVP #1/#2/#10: 注册项目 → 创建 → 启动 → 完成 → 生成报告。"""
    # 1. 注册项目（MVP #1）
    _register_project(runtime)

    # 2. 创建调查
    inv = runtime.investigations.create_investigation(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        service=SERVICE,
        symptom="HTTP 500 errors under load",
    )
    assert inv.status.value == "created"

    # 3. 编排父运行的脚本：一条带证据结论的 COMPLETED stop。
    scope = _host_scope()
    run = _make_parent_run(runtime, inv, agent_run_id="run-e2e", scope=scope)
    seed_id = _seed_run_evidence(runtime, run, inv)
    runtime.fake_provider.set_script(
        "run-e2e",
        [
            StopStep(
                stop_signal=StopSignal(
                    stop_reason=StopReason.COMPLETED, summary="investigation complete"
                ),
                conclusion=Conclusion(
                    summary="root cause: web pool exhausted",
                    evidence_ids=(seed_id,),
                ),
            )
        ],
    )

    # 4. 启动调查（走真实编排器 + 校验器 + state machine）
    started = await runtime.investigations.start(inv.investigation_id, scope)
    assert started is not None
    assert started.agent_run_id == "run-e2e"

    # 5. 验证调查状态（脚本化完成 → completed）
    inv = runtime.investigations.get_investigation(inv.investigation_id)
    assert inv.status.value in ("running", "completed", "waiting_approval")
    assert inv.status.value == "completed"
    assert started.status.value == "completed"
    assert started.stop_reason is StopReason.COMPLETED

    # 6. 生成报告（MVP #10）
    bundle = runtime.reports.generate(inv.investigation_id)
    assert isinstance(bundle, ReportBundle)
    assert bundle.markdown_path.exists()
    assert bundle.html_path.exists()
    assert bundle.metadata.symptom == "HTTP 500 errors under load"
    assert bundle.metadata.root_cause == "root cause: web pool exhausted"
    assert bundle.metadata.evidence_count >= 1


async def test_approval_flow(runtime) -> None:
    """MVP #8: 需要审批的工具调用会暂停运行并暴露待审批项。"""
    _register_project(runtime)
    inv = runtime.investigations.create_investigation(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        service=SERVICE,
        symptom="need to restart service",
    )

    scope = _host_scope()
    _make_parent_run(runtime, inv, agent_run_id="run-e2e-approval", scope=scope)
    runtime.fake_provider.set_script(
        "run-e2e-approval",
        [
            RequestToolsStep(
                tool_requests=(
                    ToolRequest(
                        tool_call_id="tc-e2e-restart",
                        tool_name="docker_action",
                        arguments={
                            "service_name": SERVICE,
                            "action": "restart",
                            "container": CONTAINER,
                        },
                    ),
                )
            )
        ],
    )

    run = await runtime.investigations.start(inv.investigation_id, scope)
    assert run.status.value == "waiting_approval"

    pending = runtime.investigations.list_waiting_approval_tool_calls()
    assert isinstance(pending, tuple)
    assert len(pending) == 1
    assert pending[0].tool_name == "docker_action"
    assert pending[0].approval_id is not None

    inv = runtime.investigations.get_investigation(inv.investigation_id)
    assert inv.status.value == "waiting_approval"


def test_report_generation_with_empty_investigation(runtime) -> None:
    """报告生成应该处理从未启动的空调查（0 证据 / 0 工具调用）。"""
    inv = runtime.investigations.create_investigation(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        service=SERVICE,
        symptom="test",
    )
    bundle = runtime.reports.generate(inv.investigation_id)
    assert isinstance(bundle, ReportBundle)
    assert bundle.markdown_path.exists()
    assert bundle.html_path.exists()
    assert bundle.metadata.symptom == "test"
    assert bundle.metadata.evidence_count == 0
    assert bundle.metadata.tool_calls_count == 0
