# Agent Runtime Phase 4 Implementation Plan

> **For agentic workers:** 本计划记录 Phase 4 已实现的 Task 1-9（provider contract、Fake Provider、tool registry/executor、orchestrator、service、source discovery、registry proposal、events/API、recovery/runtime wiring）与 Task 10（验收、文档、路线图收口）。实现已完成并通过全部测试；本文件是设计规格的可执行映射与验收记录。

**Goal:** 在 Phase 1-3（本地 runtime、持久 SSH 安全变更、混合日志与证据）之上加入有界的调查 Agent Runtime：provider-neutral 模型契约、带检查点的有界调查循环、结构化假设/结论、证据所有权校验、独立容器级 child agent、委托任务包与 child report、取消/恢复、动态源码发现与审批式注册表更新、启动恢复与有序关闭。不接入真实模型供应商、不实现 Web UI/CLI/最终报告。

**Architecture:** 复用 `runtime.py` 服务容器。Provider 只返回 proposals（工具/委托/假设/结论/停止），永不执行；执行统一走 `RemoteToolGateway` + `SessionManager` + `CommandPolicy` + `ApprovalService`（无第二条远端通道）。一切 Agent 可见外部事实先经 `EvidenceService` 落为 append-only 脱敏 `EvidenceRef`，模型只拿 id + 有界摘要。SQLite 复用同一 `runtime.db` 幂等 migration。

**Tech Stack:** Python `>=3.12,<3.13`, FastAPI `>=0.115,<1`, Pydantic `>=2.13,<3`, AsyncSSH `>=2.24,<3`, stdlib `asyncio`/`sqlite3`/`uuid`/`hashlib`, pytest 8, pytest-asyncio, Ruff。

## Global Constraints

- 不增加远端 agent、独立 worker、消息队列或多节点协调。
- 不实现真实模型供应商接入、Web 前端、交互式 CLI 或最终报告渲染。
- 不允许任意 shell、任意 Docker 参数或客户端提供 SSH 连接信息（沿用 Phase 2/3 策略门）。
- 不持久化原始日志/原始命令输出/凭据/隐藏推理；SQLite、events、HTTP 响应、错误与结论只含脱敏内容或安全摘要。
- Provider/domain 契约 `frozen=True` + `extra="forbid"`。
- 工具调用执行为 RUNNING 前先持久化（C1）；取消后落地的审批不重放危险操作（C2）。
- 项目命令使用 `UV_CACHE_DIR=.uv-cache uv run pytest ...` 与 `UV_CACHE_DIR=.uv-cache uv run ruff check ...`。

---

## File Structure

```text
apps/control-plane/src/incidentlens_control_plane/
  config.py                         有界 runtime settings（默认 run/investigation 预算、child/investigation 上限、shutdown grace）
  runtime.py                        RuntimeServices + build_runtime()：统一组装与 migration 顺序
  main.py                           lifespan：恢复 subscriptions → recovery.startup() → shutdown( investigations → subscriptions → sessions )

  investigation/
    provider.py                     provider-neutral 契约（AgentTurnRequest/Result、ToolSchema、ProviderOutputValidator、ModelProvider）
    fake_provider.py                FakeProviderRegistry + FakeScriptStep（request_tools/delegate_child/stop/malformed/schema_violation/error/crash）
    types.py                        Investigation/AgentRun/Hypothesis/Conclusion/ChildReport/DelegatedTaskPackage/ToolCall/Checkpoint/RegistryUpdateProposal/预算/用量/AgentScope
    state_machine.py                Investigation/AgentRun/ToolCall/Hypothesis 状态机（表驱动）
    guard.py                        InvestigationGuard：预算 + 证据所有权 + no-new-evidence 守卫
    tools.py                        ToolRegistry + 22 个 ToolDefinition（名称/JSON schema/scope 门/审批标志）
    tool_executor.py                ToolExecutor：evidence-first 执行、scope/路径校验、approval、UNCERTAIN
    orchestrator.py                 AgentOrchestrator：有界父/容器-child 循环、checkpoint、委托、child report、暂停/停止
    service.py                      InvestigationService：create/start/cancel/resume、审批决策联动、查询
    source_discovery.py             SourceDiscoveryService：注册边界内发现、未注册 candidate 不越权
    registry_proposals.py           RegistryProposalService：evidence-backed proposal、审批后重新校验写回
    recovery.py                     RecoveryService：启动恢复（uncertain no-replay）、有序关闭
    events.py                       InvestigationEventPublisher：共享 /api/events store + broker

  evidence/
    service.py / store.py           typed append-only EvidenceService（LOG_RECORD/COMMAND_OUTPUT/CHILD_REPORT/UNCERTAIN_STATE 等）
  routes/
    investigations.py               /api/investigations/*（创建/启动/取消/恢复/runs/children/tool-calls/checkpoints/rounds/…）
    evidence.py                     /api/evidence、/api/incidents/{id}/evidence
    approvals.py / events.py       审批与事件路由（复用）

tests/
  investigation/
    test_fake_provider.py           脚本步骤、schema 违规、error/crash
    test_state_machine.py           表驱动转移
    test_guard.py                   预算/所有权校验
    test_tool_executor.py           evidence-first 执行、scope/路径、approval、UNCERTAIN
    test_orchestrator.py            父完成、暂停、child 委托与 partial report、checkpoint
    test_recovery.py                启动恢复、有序关闭、dangerous/safe 分类、restart
    test_source_discovery.py        边界内发现、candidate 不越权
    test_store.py                   持久化、checkpoint/round/tool call/proposal
  web/
    test_investigations_api.py      REST 不泄露 arguments/raw content
    test_events_api.py              WS/events 脱敏
    test_runtime_target_resolution.py
  integration/
    test_live_agent_runtime.py      opt-in live 验收（INCIDENTLENS_RUN_LIVE_AGENT_TESTS=1）
docs/
  superpowers/specs/2026-08-12-agent-runtime-design.md
  phase-4-agent-runtime-verification.md
README.md
```

## File Responsibility Map

- `investigation/provider.py`：唯一 provider 面；模型只见有界上下文，只返回 proposals；validator 拒越权/未拥有证据/坏参数/非法停止。
- `investigation/fake_provider.py`：确定性脚本 provider；不执行工具、不写存储。
- `investigation/types.py`：domain 契约与 DB/service/routes/tests 的唯一字段来源。
- `investigation/state_machine.py` + `guard.py`：预算/所有权/转移的纯逻辑；orchestrator 把拒绝原因映射为暂停状态。
- `investigation/tools.py` + `tool_executor.py`：唯一工具注册表与执行通道；证据先于模型可见。
- `investigation/orchestrator.py`：唯一有界循环；checkpoint、委托、child report、暂停/停止。
- `investigation/service.py`：生命周期 API；审批决策重放/拒绝并 resume。
- `investigation/source_discovery.py` + `registry_proposals.py`：发现→证据→proposal→审批→重新校验写回。
- `investigation/recovery.py` + `runtime.py` + `main.py`：进程级生命周期与构造/关闭顺序。
- `routes/` 与 `investigation/events.py`：REST/WS 响应与事件只含 ID/状态/计数/脱敏摘要。

---

### Task 1: Provider-neutral 模型契约与脚本化 Fake Provider

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/provider.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/fake_provider.py`
- Create: `tests/investigation/test_fake_provider.py`

**Interfaces:**
- `AgentTurnRequest(checkpoint, investigation, hypotheses, evidence, child_reports, tool_schemas)`
- `AgentTurnResult(tool_requests, hypotheses, conclusions, child_delegation, stop_signal, usage)`
- `ProviderOutputValidator.validate(result) -> ProviderValidation`
- `ModelProvider.generate_turn(request) -> AgentTurnResult`
- `FakeProviderRegistry.set_script/pop/peek/remaining/has_script`
- `FakeScriptStep`：`RequestToolsStep`、`DelegateChildStep`、`StopStep`、`MalformedStep`、`SchemaViolationStep`、`ErrorStep`、`CrashStep`

**已实现内容（[x] 完成）**

- [x] provider 契约：所有模型 `extra="forbid"` + `frozen=True`；`AgentTurnResult` 只允许一种延续方式。
- [x] 停止原因门：`budget_*` 仅 guard/orchestrator 声明，provider 声明即拒绝。
- [x] JSON-schema 子集校验器（type/properties/required/additionalProperties/items/enum/min/max/pattern 等）。
- [x] 身份一致性检查：`request` 与 `run` 的 run/investigation/scope 必须匹配（`ProviderContextMismatch`）。
- [x] Fake Provider 脚本 registry 按 run id 寻址，跨 turn/实例共享。
- [x] 提交：`feat: add provider-neutral model contract and scripted fake provider`（2d0fc4b）、`fix: assert request/run identity consistency in provider validator`（0379773）。

### Task 2: 统一 typed append-only Evidence Service 与领域契约

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/evidence/service.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/evidence/store.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/evidence/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/types.py`

**Interfaces:**
- `EvidenceKind`：`log_record`、`command_output`、`file_snapshot`、`diff`、`validation_result`、`child_report`、`registry_discovery`、`approval_decision`、`uncertain_state`
- `EvidenceService.record_log/command_output/file_snapshot/child_report/registry_discovery/uncertain_state/approval_decision(...)`
- `EvidenceService.from_log_record(record, ...)`
- `Investigation`、`AgentRun`、`AgentScope`、`AgentBudget`、`InvestigationBudget`、`UsageCounters`、`Hypothesis`、`Conclusion`、`ChildReport`、`DelegatedTaskPackage`、`ToolCall`、`Checkpoint`、`RegistryUpdateProposal`

**已实现内容（[x] 完成）**

- [x] evidence 服务统一为 typed append-only；哈希基于脱敏内容；每个 EvidenceRef 携带 agent_run_id/incident 等所有权字段。
- [x] 领域契约强制路径绝对且无 `..`、引用唯一、JSON-compatible arguments。
- [x] `AgentRun` 的 `kind` 与 `parent_run_id` 一致性校验。
- [x] 提交：`feat: unify evidence store into typed append-only evidence service`（ae9fd78）、`fix: revalidate derived contracts and enforce hypothesis state machine`（458e0e0）、`fix: make evidence migration atomic and align log field caps`（9e70355）。

### Task 3: Agent-safe 工具注册表与 evidence-first ToolExecutor

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/tools.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/tool_executor.py`
- Create: `tests/investigation/test_tool_executor.py`

**Interfaces:**
- `ToolDefinition(tool_name, description, parameters_json_schema, allowed_scope, requires_approval, output_cap_bytes)`
- `ToolRegistry.tool_schemas(scope=...) -> tuple[ToolSchema, ...]`
- `ToolExecutor.execute(request, run, *, approval_id=None, now=None) -> ToolOutcome`
- 22 个工具名常量（`TOOL_LOG_QUERY` … `TOOL_DOCKER_ACTION`）

**已实现内容（[x] 完成）**

- [x] 22 个工具定义：日志（3）、证据（2）、注册表（2）、host 文件（4）、container 文件（4）、发现/委托（2）、shell（1，HOST 限定）、变更（2）、docker（1，静态审批）。
- [x] executor 对所有含内容结果先落 EvidenceService，模型只拿 id + 有界摘要。
- [x] 执行前二次校验 schema/scope gate/注册 service/container/路径范围；container-pin 的 run 不能降级到 host。
- [x] shell/PTTY 复用 `CommandPolicy`+`Gateway`+`SessionManager`；shell 链式/重定向元字符直接拒绝。
- [x] 远端超时/连接丢失 → `ToolUncertain` → UNCERTAIN + UNCERTAIN_STATE 证据，不自动重试。
- [x] 提交：`feat: add agent-safe tool registry and evidence-first tool executor`（9af5368）、`fix: close shell-chain and container-pin bypasses; surface wrapped timeouts as uncertain`（ef8af0f）。

### Task 4: 有界父/容器-child Orchestrator 与 InvestigationService

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/service.py`
- Create: `tests/investigation/test_orchestrator.py`

**Interfaces:**
- `AgentOrchestrator.run_investigation(investigation, parent_scope, *, parent_budget=None) -> AgentRun`
- `AgentOrchestrator.run(agent_run_id) -> AgentRun`
- `InvestigationService.create_investigation/start/cancel/resume_run`
- `InvestigationService.list_*`（runs/rounds/checkpoints/hypotheses/conclusions/children/tool_calls/proposals/delegated_tasks）

**已实现内容（[x] 完成）**

- [x] 父与每个 container child 共享同一有界循环（load→取消/预算→checkpoint→provider→validator→执行→折证据→round 后 checkpoint）。
- [x] 父可并发委托多个 container child（investigation `max_children` + orchestrator 全局信号量）；child 独立 scope/session/预算/证据。
- [x] child 崩溃/超预算/取消 → `ChildReportStatus.PARTIAL` + 证据引用，父继续。
- [x] COMPLETED 必须引用 run 证据的结论，否则转 `PAUSED_MISSING_EVIDENCE`。
- [x] 提交：`feat: add bounded parent/container-child orchestrator and investigation service`（d959309）、`fix: enforce evidence/output/tool budgets and merge concurrent investigation usage`（42fc0b8）、`fix: restore RUNNING before evidence-budget pause and reload in continue paths`（5247b4c）。

### Task 5: 预算、证据所有权与状态机

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/state_machine.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/guard.py`
- Create: `tests/investigation/test_state_machine.py`
- Create: `tests/investigation/test_guard.py`

**Interfaces:**
- `INVESTIGATION_STATE_MACHINE`、`AGENT_RUN_STATE_MACHINE`、`TOOL_CALL_STATE_MACHINE`、`HYPOTHESIS_STATE_MACHINE`
- `InvestigationGuard.check_before_model_turn/check_before_tool_execution/can_accept_output/can_spawn_child/can_accept_new_evidence/is_stalled_no_new_evidence/validate_conclusion/validate_hypothesis/validate_child_report`

**已实现内容（[x] 完成）**

- [x] 表驱动状态机；`IllegalTransition` 拒绝非法/跨枚举转移；终止态不可再执行。
- [x] guard 先 investigation 级再 run 级；拒绝原因映射暂停状态 + `StopReason`。
- [x] 结论/假设/child report 只能引用当前 run 证据。
- [x] 提交：`fix: enforce evidence/output/tool budgets and merge concurrent investigation usage`（42fc0b8）。

### Task 6: 源码发现与审批式注册表 Proposal

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/source_discovery.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/registry_proposals.py`
- Create: `tests/investigation/test_source_discovery.py`

**Interfaces:**
- `SourceDiscoveryService.discover(run, *, service_name, container=None, path=None, now=None) -> DiscoveryOutcome`
- `DiscoveryOutcome(service_name, evidence, candidates, summary)`
- `RegistryProposalService.propose(run, *, discovery_evidence_id, kind, service_name, container=None, paths=(), now=None) -> ProposalOutcome`
- `RegistryProposalService.handle_approval_decision(proposal, approval, *, now=None) -> ProposalDecisionOutcome`

**已实现内容（[x] 完成）**

- [x] 发现复用 `RemoteToolGateway` 文件操作与固定 docker 只读 argv；未注册 candidate 绝不访问，仅附带暴露它的证据 id。
- [x] container-pin 的 run 不能通过 path-based 发现枚举 host。
- [x] proposal 请求精确 single-use approval；批准后重新校验注册表、canonicalize 路径/校验容器身份、`replace()` 乐观写回 + 审计事件。
- [x] 拒绝/stale/过期审批只返回证据，不改注册表；TTL 过期拒绝。
- [x] 提交：`feat: add source discovery and approval-gated registry update proposals`（db01342）、`fix: enforce approval-before-mutation and optimistic registry writeback`（59a34b3）、`fix: refuse ttl-expired approvals before registry mutation`（b88683f）、`fix: guard path-based source discovery against container-run host enumeration`（3834a06）。

### Task 7: Investigation events、REST API 与审批联动

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/events.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/routes/investigations.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Create: `tests/web/test_investigations_api.py`
- Modify: `tests/web/test_events_api.py`

**Interfaces:**
- `RuntimeEventType.INVESTIGATION_*`、`AGENT_RUN_*`、`TOOL_CALL_*`、`CHILD_RUN_*`、`EVIDENCE_APPENDED`、`REGISTRY_PROPOSAL_*`、`RECOVERY_*`
- `InvestigationEventPublisher`（investigation_created/started/status_changed/completed/cancelled/failed、agent_run_*、tool_call_*、child_run_*、evidence_appended、registry_proposal_*、recovery_*）
- `InvestigationService.handle_approval_decision(approval_id) -> ApprovalDecisionOutcome`

**已实现内容（[x] 完成）**

- [x] 事件通过共享 `/api/events` store + broker；负载只含 ID/状态/计数/脱敏摘要。
- [x] REST：`/api/investigations` 创建/列表/获取/start/cancel/resume、runs/children/tool-calls/checkpoints/rounds/delegated-tasks/hypotheses/conclusions/proposals/evidence。
- [x] 工具调用 `arguments` 不出现在 API 响应；审批决策重放/拒绝后 resume。
- [x] 提交：`feat: add investigation events, REST API and approval linkage`（309317d）。

### Task 8: 启动恢复、有序关闭与有界 runtime settings

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/recovery.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/store.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/config.py`
- Create: `tests/investigation/test_recovery.py`

**Interfaces:**
- `RuntimeSettings`：`max_active_investigations`、`max_active_children`、`max_rounds_per_run`、`max_tool_calls_per_run`、`shutdown_grace_seconds` 等有界默认
- `RecoveryService.startup() -> RecoverySummary`
- `RecoveryService.shutdown() -> int`
- `AgentOrchestrator.drain_active_loops(timeout)` / `cancel_active_loops()`

**已实现内容（[x] 完成）**

- [x] 启动恢复：先收尾 crash-mid-cancel → 重连已决定审批 → 分类在途工具调用（危险 RUNNING → UNCERTAIN + PAUSED_UNCERTAIN_STATE；安全只读 → FAILED 可重试）。
- [x] 有序关闭：拒绝新调查 → 全部 park cancel → 宽限窗口 → 清扫残留；lifespan 顺序 investigations → subscriptions → sessions。
- [x] 工具调用执行为 RUNNING 前持久化（C1）；审批决策受 owning investigation 取消态门控（C2）。
- [x] 提交：`feat: add startup recovery, orderly shutdown and bounded runtime settings`（578bf1b）、`fix: persist RUNNING tool calls, gate approval on cancel, sweep cancel paths`（d996b05）、`fix: gate approval decisions on the owning investigation's cancel state`（e1e51a8）、`test: recovery service scenarios, restart recovery and lifespan ordering`（4cf7eb1）、`refactor: simplify recovery sweep helpers`（7368312）。

### Task 9: 完整验收、文档与路线图收口（本任务）

**Files:**
- Create: `tests/integration/test_live_agent_runtime.py`
- Create: `docs/phase-4-agent-runtime-verification.md`
- Create: `docs/superpowers/specs/2026-08-12-agent-runtime-design.md`
- Create: `docs/superpowers/plans/2026-08-12-agent-runtime-phase-4.md`
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-08-10-incidentlens-delivery-roadmap.md`

**Interfaces:**
- `INCIDENTLENS_RUN_LIVE_AGENT_TESTS=1` 门控的 opt-in live 验收（真实 SSH/Docker transport + Fake Provider 驱动）
- `build_runtime(settings, transport_factory=..., fake_provider_registry=...)`

**已实现内容（[x] 完成）**

- [x] opt-in live 测试：父读真实 host 日志 → 脱敏 LOG_RECORD 证据 → grounded 结论；并发委托两个 container child → child 检查 scoped source → 折叠 evidence-grounded report（有 docker 为 COMPLETE，否则 PARTIAL）；approval pause/resume（single-use 消费一次，远端文件真实创建）；restart checkpoint（同一 data_dir 的 fresh runtime 恢复，round 2 不重放）；uncertain no-replay（在途危险 shell 标 UNCERTAIN 后不重执行）。
- [x] 验证文档（offline 检查 + opt-in live 命令）、设计规格、实施计划、README Phase 4 能力与 roadmap 链接。
- [x] 全量 `pytest tests -q` 与 `ruff check apps/control-plane/src tests` 保持通过；live 测试默认 skip。

---

## Self-Review Checklist

- Spec coverage:
  - Provider-neutral contract、Fake Provider 脚本：Task 1。
  - 领域契约、typed evidence、所有权字段：Task 2。
  - Tool registry、evidence-first executor、shell/PTTY 审批复用、UNCERTAIN：Task 3。
  - 有界父/容器-child 循环、child report、checkpoint：Task 4。
  - 预算、状态机、no-new-evidence、ownership guard：Task 5。
  - 源码发现、审批式 registry proposal、stale/过期拒绝：Task 6。
  - events、REST API、审批联动：Task 7。
  - 启动恢复、有序关闭、有界 settings、C1/C2：Task 8。
  - opt-in live 验收、文档、路线图收口：Task 9。
- Placeholder scan: 无未解决占位；每个任务映射实际 commit。
- Type consistency: `service_name`（日志/证据/API）一致；`scope` 使用 `"host"|"container"`；evidence 只有 `content_redacted`，无 raw content；`arguments` 仅在 ToolCall 持久化，API/事件不含。
- Safety: 原始日志/命令输出/凭据/approval intent/备份明文/隐藏推理不进入 store/events/API/结论。

### Critical Files for Implementation

- `apps/control-plane/src/incidentlens_control_plane/investigation/provider.py`
- `apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py`
- `apps/control-plane/src/incidentlens_control_plane/investigation/tool_executor.py`
- `apps/control-plane/src/incidentlens_control_plane/investigation/recovery.py`
- `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- `apps/control-plane/src/incidentlens_control_plane/main.py`
