# IncidentLens 第二阶段：端到端验收与交互演示设计

## 目标

将初始化骨架收敛为可重复验证的端到端故障调查 Demo。五个既有故障场景均须在 Docker Compose 环境中完成：重置、注入、生成真实请求流量、保存遥测、发起调查、生成报告和验收。每份通过的报告都必须识别预期根因服务，并引用本次调查中生成的 Evidence ID。

本阶段同时交付自动化 Compose 级验收和交互式演示；二者共享同一套场景编排逻辑与验收契约，避免行为漂移。

## 范围与非目标

范围：

- 为控制平面增加场景生命周期 API，并使它成为唯一的运行时场景状态来源。
- 业务服务在处理请求时读取自身的非敏感故障配置，并将日志、指标和 Trace 事件 HTTP 上报至控制平面。
- 针对五个场景完成由当前遥测驱动的调查策略、结构化报告和硬性验收。
- 提供可重用的 Python 演示编排器、交互 CLI 与 Docker Compose 集成测试。

非目标：

- 不接入真实生产基础设施、自动修复能力或新的故障类型。
- 不暴露 `root_cause_label`，不允许 Agent、工具、页面、CLI 或 API 读取它。
- 不在本阶段接入或依赖外部 LLM；调查结论必须可离线、确定性地复现。

## 架构

控制平面保存场景活动状态和参数，并对业务服务提供只读运行配置接口。服务仅查询与自身相关的公开参数；场景定义中的根因标签仍仅保存在控制平面内部，且永不序列化到响应、日志或审计数据中。

```text
CLI / Compose tests
        |
        v
Control-plane scenario API -----> SQLite scenario state
        ^                                  |
        |                                  v
business services <---- runtime config API (target service + parameters only)
        |
        +---- telemetry events (logs / metrics / spans) ----> telemetry store
                                                         |
                                                         v
                                             investigation engine -> report
```

### 场景控制 API

新增 `/api/scenarios` 路由，提供：

- `GET /api/scenarios`：返回每个场景的名称、目标服务、默认参数和是否活动；不返回根因标签。
- `POST /api/scenarios/{name}/enable`：以经过 Pydantic 验证的参数启用场景。
- `POST /api/scenarios/{name}/disable`：禁用一个场景。
- `POST /api/scenarios/reset`：清除所有活动场景，并清除或隔离本次演示产生的遥测数据。
- `GET /api/scenarios/runtime/{service}`：只返回该服务当前应执行的公开配置。

场景状态须持久化，因此控制平面重启后仍可被读到。服务每次业务请求只读取自己的运行配置；配置读取失败时采用安全默认值（不注入故障），同时产生可诊断日志。演示编排器在启用场景后轮询运行配置，确认生效后才开始流量生成。

### 遥测与服务行为

`TelemetryClient` 改为异步 HTTP 上报控制平面，保留结构化 stdout 日志作为本地诊断副本。网关、订单和支付服务须对正常、延迟、错误和部署异常路径产生可区分的日志、指标及 span 状态/耗时。异常响应同样记录服务、trace ID、错误类别与调用时长。

五个场景的观测特征和预期根因服务如下：

| 场景 | 预期根因服务 | 必须出现的当前证据 |
| --- | --- | --- |
| `payment_delay` | `payment-service` | 支付 span 高耗时，支付延迟日志或延迟指标 |
| `payment_error_rate` | `payment-service` | 支付错误日志及支付/上游错误指标 |
| `db_pool_exhaustion` | `order-service` | 订单连接池耗尽日志及订单侧高耗时或错误指标 |
| `dependency_unavailable` | `order-service` | 订单依赖不可用日志、下游调用失败信息 |
| `deployment_regression` | `payment-service` | 支付服务部署记录与异常业务结果/日志 |

`deployment_regression` 的部署事件在场景启用时写入遥测存储；其余场景的异常遥测只由真实业务请求产生。

### 调查与报告

现有固定工具顺序替换为显式、确定性的证据规则：告警症状确定初始查询；每次工具结果解析出服务、错误类型、时延、Trace 关系和部署关联；规则据此生成或更新带 `candidate_service` 的假设。根因确认至少需要：

1. 候选根因服务与场景期望服务一致的可观测事实；
2. 至少一个本次调查的支持性 Evidence ID；
3. 没有能直接否定该假设的当前证据。

报告固定包含 `root_service`、`root_cause`、`evidence_ids`、`findings`、`rounds_completed` 与不确定性说明。报告守卫拒绝缺少根因服务、Evidence ID 或证据对象不属于当前 incident 的报告。历史案例仍只允许提出低置信候选，不能使任何假设直接确认。

## 统一编排器和 CLI

新增可导入的 `DemoRunner`，对一个场景执行以下流程：

1. 重置控制平面场景与演示数据；
2. 启用指定场景并确认目标服务读取到了配置；
3. 经网关发送指定数量的真实订单请求，记录成功/失败统计与 Trace ID；
4. 根据实际流量汇总生成告警；
5. 启动调查，持续运行至 `report_ready` 或 `needs_more_evidence`；
6. 将报告和验收结果返回为结构化 `DemoRunResult`。

`scripts/run_demo.py` 是该编排器的 CLI 包装：无参数时列出可选场景，指定 `--scenario` 可运行一个场景，`--all` 顺序运行五个场景；打印简洁进度、调查轮次、报告、证据引用和失败原因。它不直接访问 SQLite、容器或根因标签，只通过公开 API 调用系统。

`generate_traffic.py` 继续专注发送请求，可被编排器复用；`reset_demo.py` 改为调用重置 API，不能再要求用户手动删除数据库或 Docker 卷。

## 验收与测试

新增 Compose 端到端测试夹具：负责启动或复用已健康的四服务环境、等待 API、通过控制平面操作场景并在结束时重置。每个场景独立测试，并断言：

- 注入前后业务行为或遥测存在预期差异；
- 至少一个横跨服务的 Trace、结构化日志和指标已保存并能被只读工具查询；
- 调查到达 `report_ready`；
- `report.root_service` 等于表中的预期服务；
- `report.evidence_ids` 非空，且每个 ID 属于该 incident 的证据；
- API、工具、CLI 输出和 Web 数据中均不存在根因内部标签。

单元测试覆盖场景 API 的输入验证、持久化、服务配置过滤、遥测上报失败降级、证据规则和报告守卫。CLI 测试以假的 HTTP API 验证参数、进度和失败码；Compose 测试才覆盖真实网络链路，避免在单元测试中依赖 Docker。

## 错误处理与可重复性

- 所有控制 API 返回明确的 4xx/5xx 响应；未知场景、非法参数和未找到的服务配置不可静默成功。
- 编排器为服务健康、配置生效、遥测到达和调查完成设置有界超时；超时返回包含阶段、最近 API 响应和 incident ID 的失败结果。
- 每个场景使用确定性参数；错误率场景在验收中设为 `1.0`，以消除随机性。
- 重置操作先停用场景，再清理演示相关遥测、调查和审计记录，确保后续场景不继承前一场景的证据。
- 所有请求均生成或透传唯一 request/trace ID，便于失败时从 CLI 输出跳转到调查和遥测。

## 完成标准

以下命令和行为构成第二阶段完成条件：

- `docker compose -f infra/compose/compose.yaml up --build` 后四项健康检查均通过；
- `uv run python scripts/run_demo.py --all` 对五个场景均以成功状态结束；
- Compose 端到端测试逐一验证五个场景的故障、遥测、报告根因服务和 Evidence ID；
- `uv run ruff check . && uv run mypy packages apps && uv run pytest -q` 通过；
- 根因内部标签不经任何公开接口、工具、报告、CLI 或前端显示。
