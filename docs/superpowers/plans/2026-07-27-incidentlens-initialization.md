# IncidentLens 初始化骨架搭建计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建一个可由 Docker Compose 启动的 IncidentLens MVP 骨架，使真实三服务调用能够被采集、查询、调查和展示。

**Architecture:** gateway、order、payment 是独立 FastAPI 服务，经过 HTTP 形成真实调用链。control-plane 集中管理 SQLite 持久化、场景、只读工具、调查状态机、案例记忆与 SSE/静态页面；共享包只包含跨服务 Pydantic 契约。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、SQLite/FTS5、httpx、SSE-Starlette、pytest、Docker Compose、原生 HTML/CSS/JavaScript。

## Global Constraints

- Python 固定为 `>=3.12,<3.13`，使用 `uv` 管理依赖。
- 所有服务输出 JSON 日志，透传 `X-Request-ID` 与 `X-Trace-ID`。
- 场景根因标签只存在于控制平面内部；Agent、工具、Web 页面均不能读取它。
- 工具均为只读，Pydantic 校验参数，统一限制时间范围、结果数、文本长度、超时和重试。
- 高置信度根因报告必须引用本次调查的 `Evidence.id`；历史案例只可生成候选假设。
- 每轮迁移及每个工具调用之后均持久化检查点和审计记录。
- 不提交 `.DS_Store`、SQLite 运行时数据库或本地环境文件。

## 文件结构

| 路径 | 职责 |
| --- | --- |
| `packages/contracts/` | HTTP、遥测、工具和调查状态契约 |
| `packages/telemetry/` | SQLite schema、遥测持久化和查询仓储 |
| `packages/scenarios/` | 故障场景生命周期与流量生成 |
| `apps/{gateway,order,payment}-service/` | 真实业务调用链 |
| `apps/control-plane/` | API、工具、Agent、案例、SSE 与静态 Web |
| `infra/compose/`、`tests/` | Docker 编排和分层验证 |

### Task 1: 建立工作区与共享数据契约

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`
- Create: `packages/contracts/src/incidentlens_contracts/{__init__.py,models.py}`
- Test: `tests/contracts/test_models.py`

**Interfaces:**
- Produces: `TelemetryEvent`, `ToolResult[T]`, `Evidence`, `Hypothesis`、`InvestigationStatus`。

- [ ] **Step 1: 写失败的契约测试**

    ```python
    def test_telemetry_event_requires_trace_and_service() -> None:
        event = TelemetryEvent(event_type="log", service="order-service",
            trace_id="trace-1", occurred_at=now_utc(), payload={"message": "created"})
        assert event.trace_id == "trace-1"
    ```

- [ ] **Step 2: 运行测试确认失败**

    Run: `uv run pytest tests/contracts/test_models.py -q`

    Expected: FAIL，`incidentlens_contracts` 尚不存在。

- [ ] **Step 3: 实现最小模型和工作区配置**

    ```python
    class ToolResult(BaseModel, Generic[T]):
        ok: bool
        data: T | None = None
        error: str | None = None
        metadata: dict[str, Any] = Field(default_factory=dict)
    ```

    在工作区中声明 FastAPI、Pydantic、SQLAlchemy、httpx、pytest、ruff、mypy；忽略 `.DS_Store`、`.venv/`、`*.db`、`__pycache__/`。

- [ ] **Step 4: 验证质量门禁**

    Run: `uv sync && uv run ruff check . && uv run pytest tests/contracts/test_models.py -q`

    Expected: PASS。

- [ ] **Step 5: 提交**

    ```bash
    git add pyproject.toml uv.lock .python-version .gitignore packages/contracts tests/contracts
    git commit -m "build: initialize Python workspace and contracts"
    ```

### Task 2: 实现控制平面的遥测持久化与查询

**Files:**
- Create: `packages/telemetry/src/incidentlens_telemetry/{database.py,models.py,repository.py}`
- Test: `tests/telemetry/test_repository.py`

**Interfaces:**
- Consumes: `TelemetryEvent`。
- Produces: `record(event)`、`query_logs(...)`、`query_metrics(...)`、`get_trace(trace_id)`。

- [ ] **Step 1: 写失败的仓储测试**

    ```python
    def test_query_logs_filters_service_and_trace(repository) -> None:
        repository.record_log("order-service", "trace-a", "ERROR", "payment failed")
        rows = repository.query_logs(service="order-service", trace_id="trace-a")
        assert rows[0]["message"] == "payment failed"
    ```

- [ ] **Step 2: 运行测试确认失败**

    Run: `uv run pytest tests/telemetry/test_repository.py -q`

    Expected: FAIL，`TelemetryRepository` 不存在。

- [ ] **Step 3: 实现表结构及只读查询**

    创建 `telemetry_logs`、`metric_points`、`trace_spans`、`deployments`，全部查询使用参数化 `SELECT`。

    ```python
    def query_logs(self, *, service: str, trace_id: str | None, limit: int = 100) -> list[dict]:
        statement = select(LogRow).where(LogRow.service == service).limit(limit)
        return [row.as_dict() for row in self.session.scalars(statement)]
    ```

- [ ] **Step 4: 验证**

    Run: `uv run pytest tests/telemetry/test_repository.py -q`

    Expected: PASS，覆盖日志、指标和 Trace 聚合。

- [ ] **Step 5: 提交**

    ```bash
    git add packages/telemetry tests/telemetry
    git commit -m "feat: add SQLite telemetry repository"
    ```

### Task 3: 实现真实三服务调用链和故障场景

**Files:**
- Create: `apps/gateway-service/src/gateway_service/main.py`
- Create: `apps/order-service/src/order_service/main.py`
- Create: `apps/payment-service/src/payment_service/main.py`
- Create: `apps/shared-service/src/incidentlens_service_common/{context.py,telemetry_client.py}`
- Create: `packages/scenarios/src/incidentlens_scenarios/{models.py,service.py}`
- Test: `tests/services/test_request_flow.py`, `tests/scenarios/test_lifecycle.py`

**Interfaces:**
- Produces: 每个服务 `GET /healthz`，网关 `POST /orders`，以及 `ScenarioService.enable/disable/reset`。
- Consumes: `POST /api/telemetry/events` 和内部故障配置读取接口。

- [ ] **Step 1: 写失败的调用与重置测试**

    ```python
    async def test_order_request_keeps_trace_id(client) -> None:
        response = await client.post("/orders", headers={"X-Trace-ID": "trace-e2e"})
        assert response.status_code == 201
        assert response.json()["trace_id"] == "trace-e2e"

    def test_delay_reset(service) -> None:
        service.enable("payment_delay", {"delay_ms": 250})
        service.reset()
        assert service.active_for("payment-service") == {}
    ```

- [ ] **Step 2: 运行测试确认失败**

    Run: `uv run pytest tests/services/test_request_flow.py tests/scenarios/test_lifecycle.py -q`

    Expected: FAIL，应用和场景模块尚不存在。

- [ ] **Step 3: 实现调用链、遥测与五类故障**

    网关代理订单，订单调用支付；每跳读写 Request/Trace ID，发送 JSON log、metric、span 事件。实现 `payment_delay`、`payment_error_rate`、`db_pool_exhaustion`、`dependency_unavailable`、`deployment_regression`；真实改变服务行为并保存参数，根因标签不经任何 API 暴露。

- [ ] **Step 4: 验证**

    Run: `uv run pytest tests/services/test_request_flow.py tests/scenarios/test_lifecycle.py -q`

    Expected: PASS；Trace 跨越三个服务，场景重置后无残留。

- [ ] **Step 5: 提交**

    ```bash
    git add apps packages/scenarios tests/services tests/scenarios
    git commit -m "feat: add traced services and fault scenarios"
    ```

### Task 4: 暴露遥测 API 与审计型只读工具

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/{main.py,routes/telemetry.py}`
- Create: `apps/control-plane/src/incidentlens_control_plane/tools/{base.py,query.py}`
- Test: `tests/tools/test_read_only_tools.py`

**Interfaces:**
- Produces: 遥测接收 API、七个工具：`query_metrics`、`search_logs`、`get_slow_traces`、`get_trace`、`get_service_dependencies`、`list_recent_deployments`、`get_runbook`。

- [ ] **Step 1: 写失败的工具审计测试**

    ```python
    def test_search_logs_is_limited_and_audited(toolkit, audit_store) -> None:
        result = toolkit.search_logs(service="order-service", keyword="timeout")
        assert result.ok
        assert result.metadata["limit"] <= 100
        assert audit_store.latest().tool_name == "search_logs"
    ```

- [ ] **Step 2: 运行测试确认失败**

    Run: `uv run pytest tests/tools/test_read_only_tools.py -q`

    Expected: FAIL，工具包不存在。

- [ ] **Step 3: 实现限额、超时与统一返回**

    ```python
    class ReadOnlyTool:
        permission = "read_only"
        timeout_seconds = 3
        max_retries = 1
        async def invoke(self, args: BaseModel) -> ToolResult[Any]: ...
    ```

    输入模型限制 24 小时时间窗口、100 条记录和 16 KiB 文本；将参数/结果摘要、耗时、重试和错误写入 `tool_audits`。空结果与超时返回 `ToolResult`，不抛未处理异常。

- [ ] **Step 4: 验证**

    Run: `uv run pytest tests/tools/test_read_only_tools.py -q`

    Expected: PASS，覆盖校验、超时、审计和无写操作。

- [ ] **Step 5: 提交**

    ```bash
    git add apps/control-plane tests/tools
    git commit -m "feat: add audited read-only tools"
    ```

### Task 5: 实现检查点化的调查循环与案例记忆

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/agent/{state.py,engine.py,reporting.py}`
- Create: `apps/control-plane/src/incidentlens_control_plane/memory/{models.py,repository.py}`
- Create: `apps/control-plane/src/incidentlens_control_plane/routes/{investigations.py,cases.py}`
- Test: `tests/agent/test_investigation_engine.py`, `tests/memory/test_case_retrieval.py`

**Interfaces:**
- Produces: `start(alert)`、`run_round(incident_id)`、`resume(incident_id)`、`CaseRepository.search(...)`、`confirm(...)`。
- Consumes: 只读工具、Evidence、Hypothesis；只对 `human_verified` 建 FTS 索引。

- [ ] **Step 1: 写失败的调查与案例测试**

    ```python
    def test_engine_persists_evidence_after_two_calls(engine, checkpoints) -> None:
        state = engine.start({"service": "order-service", "error_rate": 0.17})
        state = engine.run_round(state.incident_id)
        state = engine.run_round(state.incident_id)
        assert len(checkpoints.load(state.incident_id).evidence) > 0

    def test_search_prefers_verified_case(repository) -> None:
        repository.save_case(status="human_verified", symptom="order timeout", service="order-service")
        assert repository.search("timeout", "order-service", None)[0].status == "human_verified"
    ```

- [ ] **Step 2: 运行测试确认失败**

    Run: `uv run pytest tests/agent/test_investigation_engine.py tests/memory/test_case_retrieval.py -q`

    Expected: FAIL，Agent 和记忆模块不存在。

- [ ] **Step 3: 实现状态机、报告守卫和 FTS**

    实现 `parse_alert → scope_incident → retrieve_memory → generate_hypotheses → choose_next_action → execute_tool → record_evidence → update_hypotheses → verify_root_cause → generate_report`。去重相同工具参数；记录错误/空结果/冲突，默认 8 轮。置信度大于 0.70 时必须引用当前 Evidence，否则状态为 `needs_more_evidence`。只索引 `human_verified` 案例，召回结果只能产生待验证假设。

- [ ] **Step 4: 验证**

    Run: `uv run pytest tests/agent/test_investigation_engine.py tests/memory/test_case_retrieval.py -q`

    Expected: PASS，覆盖恢复、轮次上限、冲突降置信度、证据引用与错误历史案例排除。

- [ ] **Step 5: 提交**

    ```bash
    git add apps/control-plane/src/incidentlens_control_plane/{agent,memory,routes} tests/agent tests/memory
    git commit -m "feat: add evidence-driven investigation and verified memory"
    ```

### Task 6: SSE 页面、Compose 与真实评测骨架

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/{events.py,routes/events.py}`
- Create: `apps/control-plane/static/{index.html,app.js,styles.css}`
- Create: `infra/compose/{compose.yaml,Dockerfile}`, `.env.example`
- Create: `scripts/{generate_traffic.py,reset_demo.py}`
- Create: `packages/evaluation/src/incidentlens_evaluation/{runner.py,metrics.py}`
- Create: `tests/{web/test_events.py,integration/test_compose_flow.py,evaluation/test_metrics.py}`
- Create: `README.md`, `docs/evaluation.md`

**Interfaces:**
- Produces: SSE `GET /api/investigations/{incident_id}/events`，根页面，Compose 启动命令，`run_evaluation(strategy, scenario)`。
- Event types: `state_changed`、`tool_called`、`evidence_recorded`、`report_ready`。
- Strategies: `react_no_memory`、`memory_unverified`、`incidentlens_verified`。

- [ ] **Step 1: 写失败的 SSE、Compose 与指标测试**

    ```python
    def test_metrics_use_records_not_fixed_scores() -> None:
        result = compute_metrics([
            RunRecord(root_service_expected="payment-service", root_service_actual="payment-service", tool_calls=3),
            RunRecord(root_service_expected="order-service", root_service_actual="payment-service", tool_calls=5),
        ])
        assert result.root_service_accuracy == 0.5
        assert result.average_tool_calls == 4.0
    ```

- [ ] **Step 2: 运行测试确认失败**

    Run: `uv run pytest tests/web/test_events.py tests/integration/test_compose_flow.py tests/evaluation/test_metrics.py -q`

    Expected: FAIL，SSE、Compose 与评测模块不存在。

- [ ] **Step 3: 实现展示、编排、脚本与评测**

    原生页面展示告警/注入、时间线、假设、工具摘要、Evidence、案例、报告和确认反馈，使用 `EventSource` 更新 DOM，不显示思考过程。Compose 编排四容器、健康检查和控制平面 SQLite 卷。评测至少运行五类场景并从 `evaluation_runs` 计算准确率、证据引用正确率、首次有效假设轮次、调用数、重复率、误导率和耗时，禁止固定分数。

- [ ] **Step 4: 执行最终验证**

    Run: `uv run ruff check . && uv run mypy packages apps && uv run pytest -q && docker compose -f infra/compose/compose.yaml config`

    Expected: 全部 PASS，Compose 可解析，统计源于实际运行记录。

- [ ] **Step 5: 提交**

    ```bash
    git add apps/control-plane/static apps/control-plane/src/incidentlens_control_plane/events.py infra/compose scripts packages/evaluation tests README.md docs/evaluation.md .env.example
    git commit -m "feat: add dashboard Compose demo and evaluation scaffold"
    ```

## 覆盖检查

- FR-01 至 FR-03：Tasks 2–3、6。
- FR-04 与 FR-05：Tasks 4–5。
- FR-06 与 FR-07：Task 5。
- FR-08：Task 6。
- FR-09：Task 6。
- NFR-01 至 NFR-05：Tasks 1、2、4、5、6。
- 禁止项由全局约束、Task 3 的标签隔离、Task 4 的只读工具和 Task 6 README 明确覆盖。
