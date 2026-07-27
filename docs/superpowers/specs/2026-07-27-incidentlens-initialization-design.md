# IncidentLens 初始化设计

## 目标

建立一个可通过 Docker Compose 启动的 IncidentLens MVP 骨架：真实运行的三服务实验环境产生可查询的遥测数据，由受限只读工具驱动有状态调查，并通过轻量 Web 页面展示可审计的证据链。

## 取舍与边界

- 采用 Python + FastAPI + Pydantic + SQLite；不引入 Kubernetes、OAuth、多租户、写操作工具或自动修复。
- Web 页面采用 FastAPI 托管的原生 HTML/CSS/JavaScript，使用 SSE 获取调查进度；不引入 Node.js 构建链。
- 历史案例 MVP 使用 SQLite FTS5 的关键词检索与结构化过滤；语义向量检索预留接口，但不纳入初始化骨架。
- Agent 通过显式状态机实现；模型层提供 OpenAI-compatible 适配器和确定性本地后备，确保无模型凭证时也能运行场景、验证流程与评测。
- 故障内部标签仅存于场景控制层；Agent、工具和 Web 调查视图只能读取由真实请求产生的遥测及部署记录。

## 仓库结构

```text
apps/
  gateway-service/       HTTP 入口、请求与追踪上下文透传
  order-service/         订单流程、连接池模拟、下游 payment 调用
  payment-service/       支付处理、延迟与错误率故障注入点
  control-plane/         调查 API、Agent、只读工具、记忆、SSE 与 Web 页面
packages/
  contracts/             共享 Pydantic 契约、遥测模型和 API 响应模型
  telemetry/             结构化日志、指标、Trace、部署记录的本地采集与查询
  scenarios/             场景定义、启停重置与业务流量生成
  evaluation/            基线/Agent 运行器和真实结果指标计算
infra/
  compose/               Docker Compose、环境变量与初始化挂载
tests/                   单元、集成、场景和评测测试
docs/                    架构、运行说明、场景说明与评测说明
```

每个应用独立容器化并以 HTTP 通信；共享包不包含业务服务实现。控制平面是唯一可写入 SQLite 的管理服务，三个实验服务只将其遥测事件发送至控制平面的接收接口。

## 关键组件与数据流

1. 用户通过控制平面启用、禁用或重置故障场景；场景参数写入控制平面，相关服务仅获得运行配置，不暴露根因标签。
2. 流量生成器向网关发起真实业务请求；网关、订单与支付服务透传 `request_id`、`trace_id`，产生日志、指标、span 和部署事件。
3. 控制平面将遥测标准化后持久化到 SQLite。查询端以 service、时间窗口、关键字、Trace ID 与最小耗时过滤原始数据。
4. Agent 的状态机按 `parse_alert → scope_incident → retrieve_memory → generate_hypotheses → choose_next_action → execute_tool → record_evidence → update_hypotheses → verify_root_cause → generate_report` 推进。每轮和每次工具调用后保存检查点与审计记录。
5. Agent 工具只读，统一 Pydantic 入参与受限响应包装；每项 Evidence 关联工具调用与假设，报告只能以当前 Evidence ID 支撑高置信度结论。
6. 人工确认报告后，案例才以 `human_verified` 状态进入正式检索。后续检索只能生成候选假设，必须再次以当前遥测验证。
7. Web 页面通过 REST 加载告警、注入状态、证据、工具审计、案例和报告；通过 SSE 订阅调查事件。页面不展示模型内部思考过程。

## 状态与持久化

SQLite 数据库包含：`incidents`、`investigation_checkpoints`、`hypotheses`、`evidence`、`tool_audits`、`telemetry_logs`、`metric_points`、`trace_spans`、`deployments`、`scenarios`、`incident_cases`、`case_feedback` 与 `evaluation_runs`。历史案例全文索引仅覆盖可检索的 `human_verified` 记录。

所有只读工具返回同一结构：`ok`、`data`、`error`、`metadata`。`metadata` 记录限制、截断、查询耗时和调用标识。时间窗口、最大记录数、文本长度、超时和重试次数在统一配置中限制。

## 初始化阶段的可验证交付

- Docker Compose 能启动控制平面与三个实验服务，并通过健康检查。
- 正常业务请求能跨三个服务运行并被控制平面保存为结构化日志、指标和 Trace。
- 一个延迟或错误率场景可以启用、关闭并重置，产生可查询的差异化遥测。
- 只读工具可从受控 API 查询保存的数据，并生成审计记录。
- 状态机能创建调查、至少执行两次工具调用、持久化证据与检查点，并经 SSE 向页面发送事件。
- 页面能展示调查时间线、假设、证据、工具调用和最终报告占位状态。
- 测试骨架包含服务健康检查、追踪透传、故障场景重置、工具只读约束和评测结果由运行数据产生的断言。

## 测试策略

- 单元测试覆盖 Pydantic 契约、场景状态转换、遥测归一化、查询限制、工具响应、状态机迁移与证据引用规则。
- Compose 集成测试发起真实 HTTP 请求，断言跨服务 Trace、日志和指标均可查询。
- 场景测试执行启用、流量生成、禁用和重置，验证状态无残留。
- 评测测试使用固定场景配置运行三种策略，计算真实运行记录而非固定分数。

## 不变量

- Agent 不接触场景根因标签或写操作能力。
- 历史案例不会直接成为根因结论；当前 Evidence ID 是报告置信度的唯一事实依据。
- 工具调用、状态迁移、错误、重试和截断均可审计。
- Docker Compose、种子数据、场景配置和测试命令足以在新环境复现。
