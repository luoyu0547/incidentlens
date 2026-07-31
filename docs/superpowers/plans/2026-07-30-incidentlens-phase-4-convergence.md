# IncidentLens Phase 4 真实模型收敛与可信验收实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让真实模型在收集到足够的当前事故证据后，稳定停止观测调用、生成可校验的根因提案并通过证据门禁，同时建立可信、可重复的质量与评测基线。

**Architecture:** 保留现有只读调查 Agent 负责 Skill 加载与证据采集，新建纯函数式的结论就绪判定和只暴露 `RootCauseProposal` 的结论节点。结论解析、一次修复、报告门禁、审计和检查点恢复都采用显式状态；评测结果只能从 Agent 实际输出推导，不能用场景真值补全。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、LangChain 1.3、LangGraph 1.2、SQLite、Docker Compose、pytest、Ruff、mypy。

## Global Constraints

- 所有外部工具保持只读，不增加自动修复或生产写操作。
- 根因、根因服务、证据 ID、置信度和下一步动作必须由模型生成。
- Agent 不能读取场景的 `root_cause_label`；场景真值只允许存在于验收断言侧。
- 高置信度结论必须引用当前 incident 拥有、由服务端生成的 Evidence ID。
- 历史案例只生成候选假设，不能直接通过报告门禁。
- 结论结构化输出最多尝试两次：首次输出和一次修复。
- 不新增 provider 名称分支、场景到根因的硬编码映射或 deterministic fallback。
- 每项任务均以失败测试开始，并以独立可验证的提交结束。

---

## 当前基线与范围

已完成的能力包括三服务实验环境、五类故障注入、遥测持久化、只读工具、确定性基线 Agent、LLM Agent、Skill 证据策略、LangGraph 检查点、SSE/Web Demo、案例检索和 Compose 场景测试。

当前验收缺口：

1. `graph.py` 在整个调查期间使用 `ToolStrategy(RootCauseProposal)`；真实 provider 可以持续选择观测工具并在 12 次模型调用后以 `budget_exhausted` 结束。
2. `payment_delay` 的真实运行已经收集到慢 Trace、完整 Trace、6000 ms 日志和延迟指标，但没有进入 `report_ready`。
3. `uv run mypy apps packages` 当前有 51 个错误；`uv run ruff check apps packages tests scripts` 当前有 84 个错误。
4. `tests/integration/test_scenario_acceptance.py` 未完整标记为 `integration`，导致 `pytest -m "not integration"` 仍启动 Compose，破坏本地/CI 测试分层。
5. `packages/evaluation/runner.py` 把 `root_service_actual` 默认设为场景期望服务，可能把“无报告”计为正确结果；FR-09 的 `root_cause_type_accuracy` 也尚未进入指标模型。
6. 当前工作树包含用户未提交修改和一份 `docs/phase-4-model-convergence-plan.md` 草稿；实施时不得覆盖或清理这些文件。

本阶段暂不扩展完整的案例修改、驳回、反馈分类、语义检索和 UI 知识治理闭环。这些属于 Phase 5；Phase 4 只确保现有案例先验不会绕过当前证据验证。

## 文件职责

- `agent/conclusion.py`：纯函数式材料证据判定、策略就绪判定、结论上下文和提案解析。
- `agent/graph.py`：组合调查图、结论节点、门禁节点与显式路由。
- `agent/types.py`：结论状态、错误码和 `RootCauseProposal` schema。
- `agent/state.py`：持久化审计与 API 需要的结论状态。
- `agent/runtime.py`：start/run/resume 的终态和恢复语义。
- `agent/projection.py`：LangGraph 状态到公共调查状态的唯一投影。
- `agent/middleware.py`：保留调查侧审计、证据记录、预算和门禁，不再负责强制结论 tool choice。
- `llm/canary.py`：真实 provider 的普通工具调用和单 schema 工具调用能力探针。
- `evaluation/runner.py`：从实际报告、证据和审计构造运行记录。
- `evaluation/metrics.py`：聚合 FR-09 指标，不访问场景实现。
- `tests/integration/conftest.py`：Compose 生命周期。
- `tests/integration/test_scenario_acceptance.py`：确定性 Compose 验收，必须整体带 `integration` marker。

### Task 1: 恢复可信的测试分层和质量基线

**Files:**
- Modify: `tests/integration/test_scenario_acceptance.py`
- Modify: `tests/integration/test_compose_flow.py`
- Modify: `pyproject.toml`
- Test: `tests/test_test_topology.py`

**Interfaces:**
- Consumes: pytest 已注册的 `integration`、`live_llm` markers。
- Produces: 不启动 Docker 的 `uv run pytest -m "not integration and not live_llm"`；独立的 Compose 与真实模型命令。

- [ ] **Step 1: 写入测试拓扑失败测试**

```python
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "path",
    [
        Path("tests/integration/test_scenario_acceptance.py"),
        Path("tests/integration/test_compose_flow.py"),
    ],
)
def test_integration_modules_declare_marker(path: Path) -> None:
    source = path.read_text()
    assert "pytestmark = pytest.mark.integration" in source
```

- [ ] **Step 2: 验证测试失败**

Run: `uv run pytest tests/test_test_topology.py -q`  
Expected: FAIL，指出至少一个 integration 模块没有模块级 marker。

- [ ] **Step 3: 给纯 Compose 模块增加统一 marker**

```python
pytestmark = pytest.mark.integration
```

`live_llm` 只保留在真实 provider 测试上，不能施加到确定性 Compose 测试。

- [ ] **Step 4: 分开运行本地门禁**

Run: `uv run pytest -m "not integration and not live_llm" -q`  
Expected: PASS，且输出中不出现 `docker compose`。

Run: `uv run ruff check apps packages tests scripts`  
Expected: 先记录准确错误清单；只修格式、未使用导入和明确类型问题，不做行为重构。

Run: `uv run mypy apps packages`  
Expected: 先保存 51 个错误的分类基线，再按模块逐批清零；为内部 typed package 增加 `py.typed`，不得全局 `ignore_missing_imports`。

- [ ] **Step 5: 提交测试分层修复**

```bash
git add pyproject.toml tests/integration tests/test_test_topology.py
git commit -m "test: isolate local and compose quality gates"
```

### Task 2: 引入 provider 无关的结论就绪判定

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/agent/conclusion.py`
- Create: `tests/agent/test_conclusion.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/types.py`

**Interfaces:**
- Consumes: `list[Evidence]`、已加载 Skill 的 `EvidencePolicy`。
- Produces: `ConclusionReadiness(eligible_cause_codes, eligible_evidence_ids, ready)`、`parse_proposal(tool_calls)`。

- [ ] **Step 1: 写材料证据和五类策略的参数化测试**

```python
@pytest.mark.parametrize(
    ("skill_name", "cause_code", "sources"),
    [
        ("downstream-timeout", "payment_latency_spike", {"search_logs", "get_slow_traces"}),
        ("downstream-error", "payment_service_degradation", {"search_logs", "query_metrics"}),
        ("database-pool-exhaustion", "database_connection_leak", {"search_logs", "query_metrics"}),
        ("dependency-unavailable", "network_partition", {"search_logs", "get_service_dependencies"}),
        ("deployment-regression", "bad_deployment", {"list_recent_deployments", "query_metrics"}),
    ],
)
def test_loaded_policy_becomes_ready_from_independent_material_sources(
    skill_name: str,
    cause_code: str,
    sources: set[str],
) -> None:
    readiness = evaluate_conclusion_readiness(
        incident_id="inc-1",
        loaded_skill_names=[skill_name],
        evidence=material_evidence("inc-1", sources),
        policies=load_test_policies(),
    )
    assert readiness.ready is True
    assert cause_code in readiness.eligible_cause_codes
```

同时覆盖空数据、错误 ToolResult、foreign incident、invalid arguments 和直接反证。

- [ ] **Step 2: 验证新测试失败**

Run: `uv run pytest tests/agent/test_conclusion.py -q`  
Expected: FAIL with `ModuleNotFoundError` for `agent.conclusion`。

- [ ] **Step 3: 定义结论类型和纯函数**

```python
class ConclusionStatus(StrEnum):
    NOT_READY = "not_ready"
    READY = "ready"
    ATTEMPTING = "attempting"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ConclusionReadiness(BaseModel):
    ready: bool
    eligible_cause_codes: list[str]
    eligible_evidence_ids: list[str]


class RootCauseProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_service: str = Field(min_length=1)
    cause_code: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    next_action: Literal["finish", "needs_more_evidence"]
```

`evaluate_conclusion_readiness` 只能返回候选 cause codes 和 evidence IDs，不能选择最终根因。

- [ ] **Step 4: 通过纯函数测试并提交**

Run: `uv run pytest tests/agent/test_conclusion.py tests/agent/test_evidence_rules.py -q`  
Expected: PASS。

```bash
git add apps/control-plane/src/incidentlens_control_plane/agent/conclusion.py \
  apps/control-plane/src/incidentlens_control_plane/agent/types.py \
  tests/agent/test_conclusion.py
git commit -m "feat: add evidence-driven conclusion readiness"
```

### Task 3: 拆分调查节点与结论节点

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/graph.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/middleware.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/prompts.py`
- Test: `tests/agent/test_llm_graph.py`

**Interfaces:**
- Consumes: Task 2 的 `ConclusionReadiness` 与 `RootCauseProposal`。
- Produces: `investigate -> assess_readiness -> conclude -> report_gate` 显式图；结论节点只有一个 schema tool。

- [ ] **Step 1: 写 graph 隔离失败测试**

```python
async def test_conclusion_node_cannot_call_observability_tool(agent_harness) -> None:
    graph = agent_harness.build(
        investigation_model=agent_harness.ready_investigation_model(),
        conclusion_model=agent_harness.model_calling("get_trace"),
    )
    result = await graph.ainvoke(
        agent_harness.ready_state("inc-boundary"),
        {"configurable": {"thread_id": "inc-boundary"}},
    )
    assert result["last_error_code"] == "model_output_invalid"
    assert agent_harness.tool_calls_after("conclusion_boundary_entered") == []
```

再添加断言：普通调查调用的 bind 参数不含全局 `RootCauseProposal`，结论调用只含该 schema。

- [ ] **Step 2: 验证失败行为**

Run: `uv run pytest tests/agent/test_llm_graph.py -q`  
Expected: FAIL，因为当前 `graph.py` 仍使用全局 `ToolStrategy(RootCauseProposal)`。

- [ ] **Step 3: 组合显式 StateGraph**

调查 Agent 使用：

```python
create_agent(
    model=model,
    tools=list(observability_tools),
    response_format=None,
    state_schema=IncidentAgentState,
    context_schema=InvestigationContext,
)
```

结论节点使用同一配置模型，但 bind 后仅包含 schema tool；缺失、多个或参数非法的 proposal tool call 均返回 `model_output_invalid`。

- [ ] **Step 4: 删除无效的 middleware tool_choice 覆盖**

从 `InvestigationContextMiddleware` 删除针对 `RootCauseProposal` 的 `tool_choice` 修改。保留系统 prompt 中的调查边界和只读约束，但结论 prompt 只包含 incident 摘要、eligible cause codes 和 bounded evidence summaries。

- [ ] **Step 5: 运行 graph 回归并提交**

Run: `uv run pytest tests/agent/test_llm_graph.py tests/agent/test_tool_adapter.py tests/agent/test_skills.py -q`  
Expected: PASS。

```bash
git add apps/control-plane/src/incidentlens_control_plane/agent \
  tests/agent/test_llm_graph.py
git commit -m "feat: split investigation and conclusion graph nodes"
```

### Task 4: 实现一次修复、终态、审计和恢复

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/types.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/state.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/runtime.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/projection.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/routes/investigations.py`
- Test: `tests/agent/test_recovery.py`
- Test: `tests/agent/test_runtime.py`
- Test: `tests/web/test_investigation_agent_api.py`

**Interfaces:**
- Consumes: Task 3 的显式图结果。
- Produces: 可检查点恢复的 `conclusion_status`、`conclusion_attempt_count`、eligible IDs 和 safe rejection code。

- [ ] **Step 1: 写终态矩阵测试**

```python
@pytest.mark.parametrize(
    ("responses", "status", "error_code", "attempts"),
    [
        (["valid"], "report_ready", None, 1),
        (["invalid", "valid"], "report_ready", None, 2),
        (["invalid", "invalid"], "needs_more_evidence", "model_output_invalid", 2),
    ],
)
async def test_bounded_conclusion_attempts(
    runtime_harness,
    responses: list[str],
    status: str,
    error_code: str | None,
    attempts: int,
) -> None:
    state = await runtime_harness.run_ready_incident(responses)
    assert state.status == status
    assert state.last_error_code == error_code
    assert state.conclusion_attempt_count == attempts
```

增加 checkpoint-before-conclusion、checkpoint-after-acceptance、timeout、unknown Evidence ID、直接反证和双 incident 隔离测试。

- [ ] **Step 2: 扩展状态并投影**

```python
conclusion_status: ConclusionStatus
conclusion_attempt_count: int
eligible_cause_codes: list[str]
eligible_evidence_ids: list[str]
last_report_rejection_reason: str | None
```

公共 API 至少暴露 `status` 与安全的 `last_error_code`；原始 prompt、API key、Authorization 和隐藏推理不得进入审计。

- [ ] **Step 3: 记录显式审计动作**

按顺序记录：

```text
conclusion_boundary_entered
structured_output_attempted
structured_output_invalid
report_gate_rejected
report_gate_accepted
conclusion_terminal_failure
```

第二次失败必须终止；`resume` 不得重启终态，也不得重复已经接受的 proposal。

- [ ] **Step 4: 运行恢复、并发和 API 测试并提交**

Run: `uv run pytest tests/agent/test_recovery.py tests/agent/test_runtime.py tests/web/test_investigation_agent_api.py -q`  
Expected: PASS。

```bash
git add apps/control-plane/src/incidentlens_control_plane/agent \
  apps/control-plane/src/incidentlens_control_plane/routes/investigations.py \
  tests/agent/test_recovery.py tests/agent/test_runtime.py \
  tests/web/test_investigation_agent_api.py
git commit -m "feat: persist bounded conclusion and report gate state"
```

### Task 5: 修复评测可信度和 FR-09 指标

**Files:**
- Modify: `packages/evaluation/src/incidentlens_evaluation/runner.py`
- Modify: `packages/evaluation/src/incidentlens_evaluation/metrics.py`
- Modify: `tests/evaluation/test_metrics.py`
- Modify: `docs/evaluation.md`

**Interfaces:**
- Consumes: 实际 `InvestigationState.report`、Evidence、tool audit 和模型 audit。
- Produces: 不补全结果的 `RunRecord`，包括根因服务/类型准确率、证据引用、首次有效假设轮次、工具调用、重复率、历史误导率和耗时。

- [ ] **Step 1: 写“无报告不得算正确”的失败测试**

```python
def test_missing_report_is_not_counted_as_correct(monkeypatch) -> None:
    monkeypatch.setattr(
        evaluation_runner,
        "run_investigation",
        lambda *_: finished_state(report=None),
    )
    record = evaluation_runner.run_single("react_no_memory", "payment_delay")
    assert record.root_service_actual is None
    assert record.root_cause_type_actual is None
```

- [ ] **Step 2: 扩展运行记录**

```python
class RunRecord(BaseModel):
    root_service_expected: str
    root_service_actual: str | None
    root_cause_type_expected: str
    root_cause_type_actual: str | None
    evidence_reference_correct: bool
    first_effective_round: int | None
    tool_calls: int
    duplicate_calls: int
    history_recall_count: int
    misleading_history_recall_count: int
    latency_ms: float
```

`actual` 只能从报告字段读取；没有报告就是 `None`。重复调用按 `tool_name + normalized_args` 计算，不按唯一的 `tool_call_id` 计算。

- [ ] **Step 3: 让三种策略产生真实差异**

`react_no_memory` 禁用案例检索；`memory_unverified` 允许历史候选但不做当前证据门禁；`incidentlens_verified` 使用当前证据门禁。三者必须调用同一场景运行器和同一遥测输入，不能只在构造 repository 上有名义差异。

- [ ] **Step 4: 运行评测单测并提交**

Run: `uv run pytest tests/evaluation/test_metrics.py -q`  
Expected: PASS，且无报告样本的 service/type accuracy 都为 0。

```bash
git add packages/evaluation tests/evaluation docs/evaluation.md
git commit -m "fix: derive evaluation metrics from actual agent output"
```

### Task 6: 扩展 provider canary 并完成真实 Compose 验收

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/llm/canary.py`
- Modify: `tests/live_llm/test_model_contract.py`
- Modify: `tests/integration/test_live_agent_compose.py`
- Modify: `packages/demo/src/incidentlens_demo/runner.py`
- Create after success: `docs/phase-4-live-verification.md`
- Modify after success: `README.md`

**Interfaces:**
- Consumes: 已配置的 OpenAI-compatible provider。
- Produces: 两级能力探针和一条真实 `payment_delay -> report_ready` 验收记录。

- [ ] **Step 1: 写单 schema canary 测试**

```python
def test_conclusion_canary_validates_provider_schema_call(canary_result) -> None:
    assert canary_result.normal_tool_call_passed is True
    assert canary_result.proposal_tool_call_passed is True
    assert canary_result.fallback_used is False
    assert canary_result.identity.api_key is None
```

- [ ] **Step 2: 实现只含合成 Evidence ID 的 canary**

Prompt 只提供 `ev-canary-log`、`ev-canary-trace` 和合成 cause code；输出经 `RootCauseProposal.model_validate`。日志只包含 profile、model、endpoint host、通过状态和失败码。

- [ ] **Step 3: 先通过所有非 live 门禁**

Run: `uv run pytest -m "not integration and not live_llm" -q`  
Expected: PASS。

Run: `uv run ruff check apps packages tests scripts`  
Expected: PASS。

Run: `uv run mypy apps packages`  
Expected: PASS。

- [ ] **Step 4: 独占运行确定性 Compose 回归**

```bash
INCIDENTLENS_AGENT_MODE=deterministic_baseline \
uv run pytest \
  tests/integration/test_compose_flow.py \
  tests/integration/test_scenario_acceptance.py \
  -m integration -q
```

Expected: 五类场景全部产生正确 root service、非空当前证据引用，且 API/CLI 不泄漏 `root_cause_label`。此命令不得与其他 Compose 测试并发执行。

- [ ] **Step 5: 运行真实 provider 能力与场景验收**

```bash
set -a
source .env
set +a
uv run pytest tests/live_llm/test_model_contract.py -m live_llm -vv -s
uv run pytest \
  tests/integration/test_live_agent_compose.py::test_real_model_completes_payment_delay_investigation \
  -m "integration and live_llm" -vv -s
```

Expected: Skill `downstream-timeout` 已加载；结论边界前至少两种独立材料证据；边界后没有观测调用；proposal 为模型生成并通过门禁；状态为 `report_ready`；无 fallback。

- [ ] **Step 6: 只在真实验收成功后写验证文档并提交**

`docs/phase-4-live-verification.md` 写入脱敏后的实际 profile、模型、endpoint host、模型/工具调用数、Evidence 来源、边界审计、最终状态和测试命令结果。失败时保留失败记录，不创建成功声明。

```bash
git add apps/control-plane/src/incidentlens_control_plane/llm/canary.py \
  tests/live_llm/test_model_contract.py \
  tests/integration/test_live_agent_compose.py \
  packages/demo/src/incidentlens_demo/runner.py \
  docs/phase-4-live-verification.md README.md
git commit -m "test: accept real provider conclusion convergence"
```

## 里程碑与退出标准

### M1：基线可信

- 非 integration 测试不会启动 Docker。
- Ruff 和 mypy 清零。
- 用户现有未提交修改未被覆盖。

### M2：结论路径稳定

- 调查节点没有全局 `ToolStrategy`。
- 结论节点只暴露 `RootCauseProposal`。
- 最多一次修复，终态可恢复且不重复执行。
- 报告门禁接受/拒绝原因可审计。

### M3：验收可信

- 评测不再以 expected service 补全 actual。
- FR-09 的 root service 和 root cause type accuracy 均来自实际报告。
- 五场景确定性 Compose 验收通过。
- 真实 provider canary 和 `payment_delay` Compose 验收通过。

## Phase 5 候选范围

Phase 4 完成后再独立规划：

- 案例 `draft / agent_generated / human_verified / deprecated / rejected` 全状态机；
- 根因与解决方案的确认、修改、驳回 API；
- helpful / partial / irrelevant / stale / wrong 反馈和召回采用/误导统计；
- 版本、环境、适用/不适用条件过滤及语义检索；
- Web 页面中的案例反馈、调查 JSON 导出和评测对比面板。
