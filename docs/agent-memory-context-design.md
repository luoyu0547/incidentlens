# IncidentLens Agent Memory 与 Context Compaction 设计

## 1. 设计目标

IncidentLens 的调查可能跨越多轮模型调用、多个工具、人工审批、子任务和进程重启。
完整历史不能无限回填到 Prompt，但压缩也不能让 Agent 忘记调查目标、已确认事实和未决问题。

本设计借鉴 Coding Agent 的 Harness 思路，但按事故调查领域做了两项调整：

1. IncidentLens 持久化 append-only 模型可见 transcript；调查状态、工具调用和证据结构化存储。Transcript 保留模型可见的文本块和工具调用/结果对，完整工具输出留在 EvidenceStore。
2. 历史经验不能直接成为当前事故的事实；任何结论仍必须由当前调查证据支撑。

因此，Runtime 的持久状态是事实来源，Transcript 是模型的完整对话历史，Session Memory 是
压缩后的有界投影，模型上下文是 Memory + Transcript 最近增量的临时组合。

```text
InvestigationStore / EvidenceStore（完整持久状态）
                    │
                    ▼
         Transcript（append-only 模型可见对话）
                    │
                    ▼
          Session Memory（版本化压缩记忆）
                    │
                    ▼
     固定头部 + 记忆 + 最近 transcript 增量
                    │
                    ▼
              ModelProvider turn
```

## 2. 记忆分层

### 2.1 Runtime State

完整保存 investigation、run、round、checkpoint、hypothesis、conclusion、tool call、
delegated task 和 evidence。它负责恢复与审计，不受模型上下文上限影响。

### 2.2 Transcript

每个 AgentRun 维护 append-only 模型可见 transcript，包含：

- 文本块（TextBlock）：模型输出的自然语言文本；
- 工具调用块（ToolUseBlock）：模型提议的工具调用（tool_call_id, tool_name, arguments）；
- 工具结果块（ToolResultBlock）：工具执行后的结果（status, content preview, evidence_ids）。

Transcript 严格保证 tool_use 和 tool_result 配对：每条 tool_use 消息后面必须紧跟一条
包含匹配 tool_call_id 的 tool_result 消息。`group_messages()` 函数在分组时执行配对验证，
遇到不配对的工具消息抛出 `UnpairedToolMessage` 异常。

完整工具输出通过 EvidenceStore 持久化；transcript 中的 ToolResultBlock.content 仅包含
截断后的预览（由 `tool_result_budget` 约束），`persisted_output=True` 标记该结果已落盘。

### 2.3 Session Memory

每个 AgentRun 独立维护 append-only 版本，保存：

- 当前调查目标或子任务目标；
- 已确认事实及仍活跃的假设；
- 未决问题；
- 已完成工具动作的状态摘要；
- 子任务返回的关键发现；
- 仍可按需读取的证据索引；
- 工作计划（Todo）的状态投影；
- 消耗的 transcript 序列号（through_transcript_sequence）。

Memory 只帮助模型恢复工作状态，不替代 Evidence。Memory 中的摘要不能被当作新的事实来源。

### 2.4 Active Context

每轮模型调用前从持久状态重新构建，包含固定头部和有界 transcript 增量：

- **固定头部**（固定在最前，不参与 snip/micro-compact）：
  - 当前工作计划（Todo）和最近拥有的证据引用；
  - 最新子任务报告；
  - 创建/更新计划的指令。
- **Session Memory**（最近的版本化压缩记忆）；
- **最近 transcript 增量**（通过 `group_messages(after=boundary)` 重建，经过
  `tool_result_budget` → `snip_groups` → `micro_compact` 变换）。

完整工具输出留在 EvidenceStore，通过 evidence 工具按需回读。

跨事故记忆只保存项目拓扑、稳定约束、常用排查入口和人工确认的运行手册。历史事故根因只能
作为候选线索，必须携带来源、更新时间和适用版本，并由当前调查重新取证后才能进入结论。

## 3. 压缩策略

### L0：工具结果预算

ToolExecutor 对输出设硬上限，并先将完整的脱敏结果写入 EvidenceStore。`tool_result_budget`
对 transcript 中的大结果执行截断预览（`persisted_output=True`），保持 tool_use/tool_result 配对
完整。这对应 Coding Agent 中”大结果落盘、上下文保留预览”的策略。

### L1：结构化窗口（snip + micro-compact）

`snip_groups` 按组原子性裁剪最旧的 transcript 增量（保护待审批、失败、不确定结果和
子任务通知）；`micro_compact` 对已成功的工具对用截断占位文本替换详细输出，只保留最近
`keep_recent` 个工具结果完整展示。两者都保证不拆分 tool_use/tool_result 对。

### L2：Session Memory Compaction（确定性）

当活跃上下文超出 `max_input_tokens` 预算时，从持久状态生成新的 Session Memory revision，
通过 `commit_compaction` 原子写入 memory + boundary + breaker state。该步骤不调用模型，
不产生新事实，因此可确定性测试且不会额外消耗 Token。

### L3：Semantic Compaction

`ContextCompactor` 是一个异步 Protocol（`async def compact(request) -> SessionMemory`），
无工具权限，只能返回结构化摘要。摘要必须保留证据索引、未知项和限制，经过 schema 校验后
写入新的 memory revision。连续失败（默认 3 次，可由 `agent_compact_max_failures` 配置）触发
breaker，第 N 次尝试抛出 `CompactionCircuitOpen`，阻塞后续自动语义压缩；手动 compact 可
探测已打开的 breaker，成功后重置失败计数。

在生产 `llm_agent` 模式中，runtime 会用共享的 XFYUN MaaS 配置注入
`XfyunMaaSCompactor`；它发送 tool-free 的结构化摘要请求，绝不执行模型提出的操作。fake
模式不注入网络 compactor，保持离线、确定性的测试行为。`agent_reactive_keep_recent_groups`
控制响应式压缩保留的完整 transcript 组数。模型返回 prompt-too-long 时最多执行一次响应式
压缩并重试；再次失败则安全暂停调查，不会无限重试。

### L4：Reactive Compaction

模型接口返回 prompt-too-long 时，立即触发 `reactive_compact`：保留最近 `keep_recent_groups`
个完整 transcript 组，对剩余头部执行语义压缩，然后从新边界重建上下文。每轮只允许一次响应式
重试（通过 `CompactionState.reactive_round` 防重入）；再次失败暂停调查为 `PAUSED_BUDGET`。

### L5：Todo 和 Manual Compact 工具

- `todo_write`：模型通过该工具创建/更新工作计划（`TodoItem`），原子替换当前 run 的 plan；
  evidence 引用在写入时验证所有权。计划固定在活跃上下文头部，不参与 snip/micro-compact。
- `compact_context`：模型通过该工具请求手动压缩；编排器拦截后调用
  `semantic_compact(manual=True)`，成功则重置 breaker 并记录 compact boundary。

## 4. 恢复与子任务

- 每轮前后继续写 Checkpoint；恢复时使用持久 Run State，而不是回放对话。
- Memory revision 只追加，重启后直接加载最新版本。
- 子 Agent 获得持久化的 task prompt、收窄后的 scope、独立预算和自己的 Session Memory。
- 子 Agent 结束后只把结构化报告及证据引用返回父 Agent；中间上下文不进入父上下文。

## 5. 实施状态

### 已实现

- `TranscriptMessage` 持久化模型可见对话（append-only，SQLite）；
- 工具调用/结果配对验证（`group_messages` / `UnpairedToolMessage`）；
- Token 预算 Active Context（`ContextBudget` / `ConservativeTokenEstimator` / `AgentContextManager.build`）；
- 确定性 compaction 管道：`tool_result_budget` → `snip_groups` → `micro_compact` → header + flatten；
- Session Memory 确定性构建与 `CompactBoundary` / `CompactionState` 原子持久化；
- Semantic Compaction 无工具 Protocol + schema 校验 + 配置驱动的 breaker（默认 3 次失败熔断）；
- Reactive Compaction（每轮一次，保留最近 transcript 组）；
- Todo 工具（`todo_write`）与手动压缩工具（`compact_context`）；
- 编排器持续消息循环：append-before-act、tool-result 消息、一次性 reactive retry、
  concurrency-safe 批量执行、manual compact 拦截、child isolation；
- 进程重启恢复（transcript 配对校验 → 无效尾部回退到前一 boundary）；
- 只读 inspection endpoint（`GET /api/investigations/{id}/context`）；
- Runtime 配置（token-based budget）；
- 单元测试覆盖 transcript 配对、token 预算、compaction 管道、breaker、reactive、
  orchestrator 循环、重启恢复、API endpoint。

### 下一阶段

1. 增加真实 tokenizer 或 Provider usage 校准，替换字符数估算；
3. 引入带 provenance、版本和 TTL 的 Project Memory，默认仅作为调查线索；
4. 增加长程评测：上下文压力、压缩前后事实保留、重启续跑、旧记忆污染和 Token 降幅。

## 6. 验收指标

- 压缩后调查目标、已确认事实、未决问题及证据索引不丢失；
- Provider 上下文始终不超过配置预算；
- 重启后加载同一 memory revision，并从下一轮继续；
- 子 Agent 不接收父 Agent 的完整历史，只接收任务与授权证据；
- Memory 内容不能绕过当前调查的 evidence ownership 校验；
- 压缩失败或 prompt-too-long 不产生无限重试。
