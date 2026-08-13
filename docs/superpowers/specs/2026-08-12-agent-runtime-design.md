# IncidentLens Phase 4：有界 Agent Runtime 设计

**日期：** 2026-08-12
**状态：** 已实现（Task 1-9 完成，Task 10 收口验证）
**来源：** `docs/superpowers/plans/2026-08-10-incidentlens-delivery-roadmap.md` Phase 4

## 1. 目标与范围

Phase 4 在 Phase 1-3（本地 runtime、持久 SSH 安全变更、混合日志与证据）之上加入一个**有界的调查 Agent Runtime**：一个 provider-neutral 的模型契约、一个带检查点的有界调查循环、结构化假设与结论、证据所有权校验、相互独立的容器级 child agent、委托任务包与 child report、取消/恢复、动态源码发现与审批式注册表更新，以及启动恢复与有序关闭。

本阶段不接入任何真实模型供应商；仓库内的唯一 provider 是确定性的 `FakeProvider`，由脚本步骤驱动，走与生产模型完全相同的校验、预算与工具执行路径。真实 provider 接入与最终 UI/CLI/报告属于 Phase 5。

本阶段交付：

1. Provider-neutral 模型契约（`AgentTurnRequest`/`AgentTurnResult`）与 provider 输出校验器。
2. 有界的父/容器-child 调查循环，含 round/tool/time/output/evidence/no-new-evidence 预算。
3. 结构化假设、结论、child report、委托任务包与追加式 checkpoint。
4. 证据所有权校验：模型只能引用当前 run 收集的证据。
5. 动态源码发现与审批式注册表更新 proposal。
6. 审批暂停/恢复与 single-use approval 语义。
7. 启动恢复（uncertain no-replay）与有序关闭。
8. 完整 REST API 与 durable/live 事件流。
9. 确定性 Fake Provider 脚本与 opt-in live SSH/Docker 验收。

## 2. 非目标

Phase 4 不实现：

- 真实模型供应商接入（OpenAI/Anthropic/本地模型等）。
- Web 前端、交互式 CLI、最终报告渲染。
- 远端常驻 agent、独立 worker、消息队列或多节点协调。
- 自动修复或任意 shell/任意 Docker 参数。
- 分布式租约、横向扩展或跨进程 agent 通信（只支持进程内 parent/child）。
- 原始日志、原始命令输出、凭据或隐藏推理的持久化。

## 3. 关键决策

### 3.1 Provider 只提议、永不执行

`ModelProvider.generate_turn(AgentTurnRequest) -> AgentTurnResult` 只返回 *proposals*：工具请求、child 委托、假设、结论与停止信号。provider 不执行工具、不写存储、不观察 FastAPI/SQLite。所有模型使用 `extra="forbid"` + `frozen=True`，无法携带隐藏推理或原始 transcript。

### 3.2 单一执行通道与单一事件流

ToolExecutor 复刻 Phase 2/3 的 `RemoteToolGateway`、`SessionManager`、`CommandPolicy`、`ApprovalService`、`ChangeManager` 与 `LogService`/`EvidenceService`——没有第二条远端执行路径。所有 Phase 4 事件通过共享的 `/api/events` store + broker 发布，没有第二事件流。

### 3.3 一切 Agent 可见外部事实均来自证据

工具执行结果先经过 `EvidenceService` 落为脱敏的 append-only `EvidenceRef`，模型只收到 `evidence_id` 与有界摘要。结论、假设、child report 与委托证据引用都必须属于当前 run（guard 的 ownership 校验）。空引用集合视为 missing-evidence 停止，而不是伪造。

### 3.4 父与 child 完全隔离

child 只运行自己的有界循环、自己的 scope/session/预算/证据包；关闭 child 的 container session 永不触碰 host session。child 以结构化 `ChildReport`（complete/partial）+ `EvidenceReference` 回报父，父绝不接收原始 transcript。child 不能委托孙 agent（guard 拒绝）。

### 3.5 确定性 Fake Provider 驱动验收

`FakeProvider` + `FakeProviderRegistry` 按 run id 存脚本步骤。测试预先注册脚本，逐轮 replay，可精确断言每一步执行的 provider 输出。Fake 本身不执行工具、不写存储，只产出 proposals，由真实 `ProviderOutputValidator`/`InvestigationGuard` 校验。

### 3.6 崩溃后不自动重放危险操作

工具调用在执行为 RUNNING 前先持久化（C1）。进程中断后，启动恢复把危险的 RUNNING 调用标为 UNCERTAIN（配 UNCERTAIN_STATE 证据），run 停在 `PAUSED_UNCERTAIN_STATE`，绝不自动重放；安全的只读调用标为 FAILED（可重试）。审批决策必须在运行被取消前落地，避免取消后仍执行危险操作（C2）。

## 4. 领域模型

所有契约不可变（`frozen=True`）、拒绝未知字段（`extra="forbid"`）。字段是 DB、service、routes、tests 的唯一契约。

### 4.1 枚举

- `StopReason`：`completed`、`budget_rounds`、`budget_tool_calls`、`budget_time`、`budget_output`、`budget_evidence`、`budget_children`、`budget_no_new_evidence`、`missing_evidence`、`pending_approval`、`uncertain_state`、`cancelled`、`failed`。
- `AgentRunKind`：`parent`、`child`。
- `ChildReportStatus`：`complete`、`partial`。
- `RegistryUpdateKind`：`container_registration`、`path_extension`。
- `RegistryProposalStatus`：`pending`、`approved`、`rejected`、`stale`。

### 4.2 预算与用量

- `InvestigationBudget`：跨所有 run 的全局预算（`max_rounds`=32、`max_tool_calls`=64、`max_children`=4、`max_wall_clock_seconds`、`max_total_output_bytes`=16 MiB、`max_evidence`=300、`max_no_new_evidence_rounds`=3）。
- `AgentBudget`：单个父/child run 预算（`max_rounds`=8、`max_tool_calls`=16、`max_output_bytes_per_tool`=512 KiB、`max_total_output_bytes`=4 MiB、`max_evidence`=100、`max_no_new_evidence_rounds`=3）。
- `UsageCounters`：`rounds`、`tool_calls`、`children`、`wall_clock_seconds`、`total_output_bytes`、`evidence_count`、`consecutive_no_new_evidence_rounds`。
- `ProviderUsage`：单 turn 的 `input_tokens`、`output_tokens`、`output_bytes`。

`RuntimeSettings` 暴露有界默认值（`default_run_budget()`/`default_investigation_budget()`），runtime 绝不带无界预算启动。

### 4.3 AgentScope

```python
AgentScope(project_id, target_id, scope: LogScope, service_name=None,
           container_name=None, allowed_host_paths=(), allowed_container_paths=())
```

- CONTAINER scope 必须同时设置 `service_name` 与 `container_name`；HOST scope 不得设置。
- 路径必须是绝对路径且不含 `..`。
- child scope 必须是父 scope 的合法收窄：project/target 必须一致；CONTAINER 父只能委托 CONTAINER child（同一 service/container，路径子集）；HOST 父可委托 CONTAINER child（路径在各自命名空间）。

### 4.4 结构化输出

- `Hypothesis`：`summary`、`facts`、`inferences`、`unknowns`、`evidence_ids`（只引用本 run 证据）。
- `Conclusion`：`evidence_ids` 可为空（missing-evidence 信号，而非伪造）。
- `ChildReport`：child 回报父的证据基报告；partial 报告记录已收集证据与停止原因。
- `DelegatedTaskPackage`：父给 child 的 scoped、bounded 上下文（`task_prompt`、`scope`、`budget`、`evidence_ids`）。
- `ToolCall`：一次计划的/已执行的工具调用；`arguments` 原样持久化以便审批后精确重放，但事件负载绝不包含 `arguments`。
- `Checkpoint`：`sequence`、`round_number`、`status`、`usage`；每次循环在 model turn 前（奇数 seq）与 round 后（偶数 seq）追加。
- `RegistryUpdateProposal`：证据支撑的注册表收窄/扩展提议，审批后写回。

## 5. Provider 契约

`investigation/provider.py` 是 provider-neutral 契约所在：

- `AgentTurnRequest`：一个 turn 的完整有界上下文——`checkpoint`（RunCheckpoint）、`investigation`（InvestigationSnapshot）、`hypotheses[-64:]`、`evidence[-100:]`（EvidenceReference）、`child_reports[-8:]`、`tool_schemas`（本 run scope 可见的工具）。**不含原始 transcript、隐藏推理或越界引用。**
- `AgentTurnResult`：`tool_requests`、`hypotheses`、`conclusions`、`child_delegation`、`stop_signal`、`usage`。同一 turn 只能选择一种延续方式（工具 / 委托 child / 停止），最多一种。
- `ProviderOutputValidator`：orchestrator 侧门卫。拒绝：未允许列表的工具、JSON schema 不合格的参数、越界 scope、引用未拥有证据的假设/结论/委托、provider 不可声明的停止原因、空 turn、输出超预算。它与 `InvestigationGuard` 共享预算与所有权检查。
- `ModelProvider`：唯一抽象接口 `generate_turn`。

### 5.1 Provider 可声明的停止原因

`_PROVIDER_DECLARABLE_STOPS` = `{completed, missing_evidence, pending_approval, uncertain_state, cancelled, failed}`。`budget_*` 原因只能由 guard/orchestrator 检测，模型声明即被拒绝。

### 5.2 Tool 契约

`investigation/tools.py` 定义 21 个工具（名称、JSON schema、scope 门、审批标志），`ToolRegistry` 把它们物化为 provider 可见的 `ToolSchema`，并绑定 handler：

| 分组 | 工具 |
|---|---|
| 日志 | `log_query`、`log_search`、`log_context` |
| 证据 | `evidence_read`、`evidence_list` |
| 注册表 | `registry_info`、`service_info` |
| 文件（host） | `host_read`、`host_list`、`host_search`、`host_stat` |
| 文件（container） | `container_read`、`container_list`、`container_search`、`container_stat` |
| 发现/委托 | `source_discover`、`delegate_child` |
| shell | `shell_exec`（仅 HOST scope） |
| 变更 | `file_edit`、`file_write` |
| Docker | `docker_action`（静态要求审批） |

`ToolExecutor` 是 evidence-first 执行器：任何含内容的结果先经 `EvidenceService` 落为脱敏证据，模型只拿到 id + 有界摘要。执行前再次校验 JSON schema、scope gate、注册 service/container 与 run 的 allowed paths；shell/PTTY 复用 `CommandPolicy` + `Gateway` 审批路由 + `SessionManager` SSH 通道。远端超时/连接丢失记为 `UNCERTAIN`（含 UNCERTAIN_STATE 证据），绝不自动重试。

## 6. 状态机

`investigation/state_machine.py` 是表驱动的唯一事实来源；非法转移抛 `IllegalTransition`，终止态不可再执行。

- `InvestigationStatus`：`created → running → {waiting_approval, waiting_registry_update, paused_budget, paused_missing_evidence, paused_uncertain_state, cancel_requested, failed, completed}`。
- `AgentRunStatus`：`created → running → {waiting_tool, waiting_children, waiting_approval, paused_*, cancel_requested, failed, completed}`。
- `ToolCallStatus`：`planned → {waiting_approval, running, succeeded, failed, uncertain, cancelled}`（`running → waiting_approval` 允许执行器先把调用标记 RUNNING 再停在审批）。
- `HypothesisStatus`：`proposed/active → {confirmed, refuted, superseded}`（吸收态）。

`InvestigationGuard` 是纯预算/所有权守卫：`check_before_model_turn`、`check_before_tool_execution`、`can_accept_output`、`can_spawn_child`、`can_accept_new_evidence`、`is_stalled_no_new_evidence`、`validate_conclusion/hypothesis/child_report`。拒绝原因映射为暂停状态 + `StopReason`。

## 7. Orchestrator 循环

`AgentOrchestrator` 运行有界循环，父与每个 container child 共享同一循环：

1. 加载最新 run + investigation。
2. 处理取消、终止态、guard 预算检查（先 investigation 级，再 run 级）。
3. 追加 `before_model_turn` checkpoint（奇数 seq）。
4. 调用 provider 并持久化结构化 round summary。
5. 用 `ProviderOutputValidator` 校验 proposals（传入完整 `AgentRun`）。
6. 执行工具 / 委托 container child / 应用停止信号。
7. 把新证据与假设折入 run，更新累计用量。
8. 追加 round 后 checkpoint（偶数 seq），决定继续或安全停止。

父可并发委托多个 container child（受 investigation 的 `max_children` 与全局信号量限制）。child 运行自己的循环、自己的 scope/session/预算/证据包，返回 `ChildReport`（取消/崩溃/超预算时为 partial）+ 证据引用。关闭 child 的 container session 不触碰 host session。

停止/暂停路径：

- `COMPLETED`：必须有引用 run 证据的结论，否则转为 `PAUSED_MISSING_EVIDENCE`（never fabricate）。
- `MISSING_EVIDENCE`/`BUDGET_NO_NEW_EVIDENCE` → `PAUSED_MISSING_EVIDENCE`。
- `PENDING_APPROVAL` → `WAITING_APPROVAL`。
- `UNCERTAIN_STATE` → `PAUSED_UNCERTAIN_STATE`。
- `CANCELLED`/`FAILED` → 终态。
- 预算耗尽（round/tool/time/output/evidence/children/no-new-evidence）→ 相应暂停。

## 8. Checkpoint 与恢复

- 每个 round 追加两个 checkpoint（before/post），`sequence` 为 `2*round-1` 与 `2*round`；恢复重入已存在 sequence 视为幂等（`CheckpointConflict`）。
- `InvestigationStore` 持久化 run、investigation、checkpoint、round、hypothesis、conclusion、tool call、delegated task、proposal。
- 审批决策与 resume 从最新 checkpoint 重新进入循环；`WAITING_CHILDREN` 父重新发现未完成的 child 并重建其 container session。
- `RecoveryService.startup()`：先收尾 crash-mid-cancel 的 run，再重连已决定但未处理的审批，再分类在途工具调用（危险 RUNNING 调用 → UNCERTAIN + `PAUSED_UNCERTAIN_STATE`；安全只读调用 → FAILED 可重试）。`shutdown()`：拒绝新调查 → 全部标记取消 → 宽限窗口等待 active loops 收尾 → 清扫残留（不可确认的危险调用 → UNCERTAIN，其余 → CANCELLED）。

## 9. 审批、安全与 uncertain-state

- 工具执行前把调用标记 `RUNNING`（C1），崩溃后可分类。
- shell/PTTY、file_edit/file_write（受保护路径）、docker_action 与 registry 更新都需要精确 single-use approval；`CommandPolicy` 的 auto-read 命令直接执行，FORBIDDEN 命令永不执行。
- 审批决定 `handle_approval_decision` 把匹配的 `WAITING_APPROVAL` 工具调用以完全相同的 `arguments` 重放（审批已消费），或把匹配的 registry proposal 交给 `RegistryProposalService` 应用。
- 取消后落地的审批绝不重放危险操作（C2）：run 或其 investigation 处于取消/终止态时，审批只把调用标为 CANCELLED。
- 不确定远端状态：超时/连接丢失无法确认结果的调用 → UNCERTAIN + UNCERTAIN_STATE 证据，run 停 `PAUSED_UNCERTAIN_STATE`，恢复时重估暂停条件，但 UNCERTAIN 调用本身不再执行。
- 事件负载与错误摘要不含原始日志、原始命令输出、凭据、canonical approval intent、备份明文或隐藏推理。

## 10. 源码发现与注册表 Proposal

`SourceDiscoveryService` 在注册边界内收集 typed、脱敏证据（`registry_discovery`、`command_output`、`file_snapshot`），复用 `RemoteToolGateway` 文件操作与固定 docker 只读 argv（`ps`/`inspect`/`compose config`）。未注册的容器/路径只作为 candidate 暴露，绝不访问，并附带暴露它的证据 id。

`RegistryProposalService` 把 candidate 变成证据支撑的 `RegistryUpdateProposal`，请求精确 single-use approval；批准后重新校验当前注册表、canonicalize 路径 / 校验容器身份、经 `ProjectRegistryStore.replace`（乐观 updated_at 冲突检测）原子写回并发布审计事件。agent/model 永不直接改注册表；拒绝或 stale 决定作为证据返回，父可继续原权限或带 limitation 停止。

## 11. API 与 Events

REST 路由（`routes/`）：

- `POST /api/investigations`、`GET /api/investigations`
- `GET /api/investigations/{id}`、`POST .../start`、`POST .../cancel`、`POST .../resume`
- `GET /api/investigations/{id}/runs`、`/runs/{run_id}`、`/runs/{run_id}/children`
- `GET /api/investigations/{id}/runs/{run_id}/tool-calls`、`/checkpoints`、`/rounds`、`/delegated-tasks`
- `GET /api/investigations/{id}/hypotheses`、`/conclusions`、`/proposals`、`/evidence`
- `GET /api/evidence/{evidence_ref_id}`、`GET /api/incidents/{incident_id}/evidence`
- 复用 `approvals`、`events`、`changes`、`logs`、`projects`、`remote-sessions` 路由

所有请求模型 `extra="forbid"`；工具调用的 `arguments` 不出现在 API 响应中。

Durable + live 事件（`events/types.py`）：

- `investigation.*`：created/started/status_changed/completed/cancelled/failed
- `agent_run.*`：started/status_changed/completed/failed/cancelled
- `tool_call.*`：started/status_changed/completed
- `child_run.*`：started/completed
- `evidence.appended`
- `registry_proposal.*`：created/decided
- `recovery.*`：started/completed

负载只含 ID、状态、计数与有界脱敏摘要。

## 12. Runtime 生命周期

`build_runtime()` 按依赖顺序构造：stores + broker → approvals → sessions → changes → gateway → logs/subscriptions → evidence_service → executor → fake_provider → orchestrator → source_discovery → registry_proposals → investigation_service → recovery。`RuntimeServices` 暴露全部服务。

FastAPI lifespan 顺序：`subscriptions.start_active_opt_in()` → `recovery.startup()` → 服务请求 → 关闭时 `recovery.shutdown()`（investigations → subscriptions → sessions）。

## 13. 测试策略

### 13.1 单元测试

- `tests/investigation/test_fake_provider.py`：脚本步骤、schema 违规、错误/崩溃。
- `tests/investigation/test_state_machine.py`：表驱动转移与非法转移。
- `tests/investigation/test_guard.py`：预算、所有权、child report 校验。
- `tests/investigation/test_tool_executor.py`：evidence-first 执行、schema/scope/路径校验、approval、UNCERTAIN。
- `tests/investigation/test_orchestrator.py`：父完成、缺失证据暂停、预算耗尽、审批暂停、不确定暂停、child 委托与 partial report。
- `tests/investigation/test_recovery.py`：启动恢复、有序关闭、dangerous/safe 分类。
- `tests/investigation/test_source_discovery.py`：注册边界内发现、candidate 不越权。
- `tests/investigation/test_store.py`：持久化、checkpoint、round、tool call、proposal。
- `tests/web/test_investigations_api.py`、`tests/web/test_events_api.py`：REST/WS 不泄露原始内容。
- `tests/web/test_runtime_target_resolution.py`、`tests/test_app.py`：lifespan 顺序与恢复。

### 13.2 Opt-in live 验收

`tests/integration/test_live_agent_runtime.py` 默认跳过，仅在 `INCIDENTLENS_RUN_LIVE_AGENT_TESTS=1` 且 Docker/SSH 可用时运行，用 Fake Provider 驱动真实 `AsyncSshTransport`：

1. 父读真实 host 日志 → 脱敏 LOG_RECORD 证据 → grounded 结论。
2. 并发委托两个 container child → child 检查 scoped source → 折叠 evidence-grounded report（有 docker 为 COMPLETE，否则 PARTIAL）。
3. approval pause/resume：真实 shell 命令，single-use 审批消费一次，远端文件真实创建。
4. restart checkpoint：同一 data_dir 的 fresh runtime 恢复 parked run，round 2 不重放（checkpoints 1-4、rounds 1-2）。
5. uncertain no-replay：在途危险 shell 调用经启动恢复标为 UNCERTAIN，resume 后不重执行。

## 14. Phase 5 边界

Phase 5 负责把本阶段稳定的 provider-neutral 契约、REST/events 与结构化结果接到：

- 真实模型 provider 适配器（走 `ModelProvider`，实现 `generate_turn`）。
- 交互式 CLI、本地 Web UI、共享调查时间线、日志视图、审批与 diff 屏、源码路径管理。
- 最终报告渲染与完整 Docker Compose 验收环境。

本仓库仍不含：真实 provider、Web UI、CLI、最终报告渲染。

## 15. Phase 4 完成门槛

1. Provider-neutral 契约与确定性 Fake Provider 可驱动完整父/子调查。
2. 父可并发委托 container-scoped independent child，并接收 evidence-grounded complete/partial reports。
3. checkpoint、cancel、resume 与 Runtime restart recovery 可重复验证。
4. budget exhaustion、missing evidence、pending approval 与 uncertain remote state 均安全停止。
5. shell/PTTY、变更、Docker mutation 与 registry update 无法绕过既有 policy/approval。
6. 所有 Agent 可见外部事实均来自 append-only、redacted evidence；所有结论引用通过 ownership 校验。
7. 动态源码发现可形成审批式 registry proposal，未批准前不扩大权限。
8. Phase 4 REST/events/structured result 对 Phase 5 稳定可用，仓库不含真实 provider、Web UI、CLI 或最终报告渲染。
