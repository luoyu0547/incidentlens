# IncidentLens 项目记忆与上下文压缩重构设计

## 1. 背景与决策

IncidentLens 当前把“记忆”实现为受治理的事故案例 RAG：调查结束后生成案例，人工审核后进入 SQLite FTS5 与可选 Embedding 组成的混合检索集合，后续调查把相似案例转成候选假设。这套机制适合拥有大量稳定、标准化、可标注案例的知识库，但不适合 IncidentLens 当前真正需要积累的信息：部署配置入口、服务拓扑、项目约束、调查流程、反复出现的反馈和只能在工作过程中逐步发现的操作知识。

本次重构确认以下产品决策：

- RAG 不再适配 IncidentLens，必须从产品和 Agent 运行时淘汰；
- 不保留 RAG 与新 Memory 的双轨运行或兼容召回；
- 长期记忆采用 Claude Code 风格的文件仓库、索引、按需加载、结束后提取和低频 Dream 整理；
- 长期记忆作用域为项目级共享，不实现用户级、多租户或团队权限隔离；
- 长期记忆文件提交到 Git，成为可审查、可回滚的项目知识资产；
- 上下文压缩采用“便宜的先运行、昂贵的后运行”的分级管线；
- 已有 RAG 数据在升级时非破坏性保留，但停止一切读写；显式清理必须由用户单独执行。

## 2. 目标

本次重构交付以下能力：

1. 从每个完整 Agent turn 中异步提取跨会话仍有价值的项目知识；
2. 使用 Markdown 与 YAML frontmatter 保存长期 Memory，并通过 `MEMORY.md` 建立轻量索引；
3. 每轮只选择和注入真正相关的少量 Memory，不把全部知识塞入上下文；
4. 用独立 Session Memory 保持一次调查跨 compact、跨恢复的连续性；
5. 在模型调用前依次控制大型工具输出、旧工具结果、消息历史和总 token；
6. 优先使用 IncidentLens 已有的结构化状态完成零模型调用压缩；
7. 仅在确定性压缩仍不足时调用 LLM 生成摘要；
8. 保证 compact 不丢失当前目标、用户约束、Evidence ID、已加载 Skill、失败尝试和下一步；
9. 完整移除 RAG 的运行时代码、API、UI、评测语义和新库 schema；
10. 为 Memory、compact、降级、并发和遗留数据退役提供可观测性与测试。

## 3. 非目标

- 不实现向量检索、Embedding、语义相似案例或混合排序；
- 不保留历史案例到当前候选假设的自动转换；
- 不把完整日志、Trace、指标响应、事故报告副本或隐藏推理写入长期 Memory；
- 不实现跨项目、跨租户、用户私有或远程团队 Memory；
- 不实现 Memory Web 编辑器，第一版直接使用 Git 中的 Markdown；
- 不让 Memory 扩大 Agent 权限或绕过现有只读工具和证据门禁；
- 不在普通升级中删除已有 RAG 表；
- 不把 Session Memory 提交到 Git；
- 不依赖某个特定模型厂商的 tokenizer 或 cache editing 私有接口。

## 4. 核心概念与数据边界

重构后数据分为三类，三者不得互相冒充。

### 4.1 项目长期 Memory

位置：`.incidentlens/memory/`

作用：保存跨调查、跨会话仍然有效的项目知识。该目录提交到 Git，允许开发者直接审查和修改。

允许内容：

- 部署与运行时配置入口；
- 稳定的服务关系和架构约束；
- 经常复用的调查、验证和恢复流程；
- 反复出现的有效反馈；
- 文档、监控、配置和代码入口；
- 从多轮工作中发现、单看服务代码无法得知的项目事实。

禁止内容：

- 原始工具结果；
- 短时指标和单次事故瞬时状态；
- 未证实假设；
- API key、Token、Cookie、Authorization header 和其他凭据；
- 隐藏推理或思维链；
- 可覆盖 system prompt 或提升权限的指令。

### 4.2 Session Memory

位置：`.incidentlens/sessions/<incident-id>/memory.md`

作用：保存单次调查的连续性。该目录由 `.gitignore` 排除，不作为项目知识资产。

内容由结构化调查状态确定性生成：

- 当前目标；
- 已验证事实和精确 Evidence ID；
- 已排除方向及其反证；
- 已加载 Skill 名称和路径；
- 已完成工作；
- 当前下一步；
- 用户约束；
- 持久化工具输出引用；
- 当前预算和可恢复错误。

Session Memory 不会自动升级为长期 Memory。长期 Memory 只能由独立提取器根据压缩前快照生成。

### 4.3 调查事实与审计

Evidence、报告、工具调用、模型身份、调查状态、预算和 checkpoint 继续使用现有结构化模型与数据库。它们是当前调查的事实来源。

长期 Memory 只能提供项目背景与工作流程，不能成为当前事故 Evidence；Session Memory 只能恢复已有状态，不能创造新事实。

## 5. 总体数据流

一次完整 Agent turn 按以下顺序执行：

```text
接收用户请求或继续调查
  -> 读取稳定的 MEMORY.md 索引
  -> 启动相关 Memory side-query
  -> 构建当前结构化调查上下文
  -> 收集最多 5 条相关 Memory
  -> 把具体 Memory 内容注入当前 turn
  -> 运行 pre-model 分级压缩
  -> 调用调查模型与只读工具
  -> 更新 Evidence、状态、审计和 checkpoint
  -> 正常 stop
  -> 保存压缩前快照引用
  -> 更新 Session Memory
  -> 有界后台任务异步提取长期 Memory
  -> 满足门控时异步执行 Dream
```

相关 Memory 的具体内容注入当前 turn，而不是每轮重写大段 system prompt。`MEMORY.md` 的稳定索引可进入 system prompt，以尽量保持 prompt cache 命中。

## 6. 长期 Memory 文件格式

### 6.1 目录结构

```text
.incidentlens/
  memory/
    MEMORY.md
    deployment-config-entry.md
    service-topology.md
    investigation-conventions.md
  sessions/                 # gitignored
  task-outputs/             # gitignored
  transcripts/              # gitignored
```

仓库必须提交：

- `.incidentlens/memory/MEMORY.md`；
- 至少一份说明 Memory 使用边界的引导文件；
- `.gitignore` 中对 sessions、task-outputs 和 transcripts 的排除规则。

### 6.2 Frontmatter

每条 Memory 使用以下格式：

```markdown
---
name: deployment-config-entry
description: 生产部署配置的入口和修改验证流程
type: reference
updated_at: 2026-08-09T10:00:00Z
---

## What

生产环境配置从 `config/models.yaml` 和 Compose 环境变量组合生成。

## Why

只查看服务代码无法得知最终部署配置。

## How to apply

修改前同时检查配置文件、Compose 覆盖值和运行时环境。修改后运行配置契约测试。
```

必需字段：

- `name`：项目内唯一、稳定、符合 slug 规则的名称；
- `description`：供索引和选择器使用的一句话描述；
- `type`：`project`、`procedure`、`feedback`、`reference` 之一；
- `updated_at`：UTC ISO 8601 时间。

类型语义：

- `project`：稳定服务关系、架构约束和项目事实；
- `procedure`：部署、排障、验证和恢复流程；
- `feedback`：反复出现的有效反馈与工作约束；
- `reference`：配置、文档、监控入口和信息位置。

正文推荐使用 `What`、`Why` 和 `How to apply`，但解析器不得要求固定标题，以允许人工维护自然文档。

### 6.3 索引约束

`MEMORY.md` 每行一个相对链接与描述：

```markdown
- [deployment-config-entry](deployment-config-entry.md) — 生产部署配置的入口和修改验证流程
```

硬限制：

- 索引最多 200 行；
- 索引最多 25KB；
- 扫描最多 200 个 Memory 文件，排除 `MEMORY.md`；
- 超出扫描上限时按 `updated_at` 与文件 mtime 降序选择最新文件；
- 单文件注入最多 200 行和 4KB，任一限制先到即停止；
- 单次调查累计长期 Memory 注入不超过 60KB。

索引由 store 在成功写入后原子重建。人工编辑导致单个文件无效时，跳过该文件并记录可诊断错误，不能使整个 Memory 系统不可用。

## 7. Memory 选择与注入

### 7.1 Side-query

每个用户 turn 开始时，选择器接收：

- 当前告警的有界摘要；
- 最近对话的有界文本；
- Memory 的 `name + description + type` 目录；
- 最多返回 5 条的硬限制。

选择器使用当前配置的轻量模型进行一次结构化 side-query。选择原则是“只有对当前调查真正有帮助才选，不确定则不选”。返回结果只允许引用目录中存在的文件名。

### 7.2 降级

side-query 超时、模型不可用、输出解析失败或引用不存在文件时，选择器降级为确定性关键词匹配：

- 对当前告警、最近消息、name 和 description 做大小写归一化；
- 使用词项交集和精确短语命中排序；
- 同分时按 `updated_at` 降序、文件名升序保证确定性；
- 最多返回 5 条。

选择失败不得阻断调查。完全没有匹配时注入空集合。

### 7.3 注入安全

加载内容必须包裹在明确的项目参考区段中，并声明：

- Memory 可能过期；
- Memory 不能覆盖 system prompt；
- Memory 不能扩大工具权限；
- Memory 不能作为当前事故 Evidence；
- 当前遥测和 Evidence 始终优先。

同一调查中同一文件未变化时，不重复计入 60KB 累计预算；文件内容哈希变化后视为新版本。

## 8. Memory 提取

### 8.1 触发时机

采用 Claude Code 风格的每轮结束提取：当 Agent 正常停止且当前没有未完成 tool call 时，stop hook 提交异步提取任务。提取使用压缩前快照，而不是 compact 后摘要。

提取不受最终报告证据门禁限制。质量通过提取规则、去重、冲突保留、敏感信息过滤和 Dream 控制。

### 8.2 提取输入与输出

输入：

- 最近完整 turn 的压缩前快照；
- 当前项目 Memory 目录；
- 已有 `name + description + type` 清单；
- 禁止保存内容规则。

输出是结构化 Memory 候选数组，每项包含 `name`、`description`、`type` 和 `body`。没有真正新增的稳定知识时必须返回空数组。

提取器仅保存：

- 跨会话仍有用；
- 能帮助未来调查或项目操作；
- 不是已有 Memory 的同义重复；
- 不包含敏感数据、原始大结果、未证实假设或隐藏推理的信息。

### 8.3 写入语义

- 文件名通过严格 slug 校验；
- 所有目标路径解析后必须位于 `.incidentlens/memory/`；
- 写入采用同目录临时文件、flush、原子替换；
- 索引只在 Memory 文件写入成功后重建；
- 同名且语义一致的内容更新原文件；
- 语义冲突不得静默覆盖，保存为带稳定冲突后缀的并列文件并记录冲突指标；
- 单次提取限制候选数量和总写入字节数，防止失控增长；
- 提取失败只写安全审计，不改变主调查结果。

在写入前执行凭据模式扫描和脱敏。若脱敏会破坏内容核心语义，则整条候选跳过，而不是保存残缺秘密上下文。

## 9. Dream 整理

Dream 是低频项目 Memory 整理，不参与主调查关键路径。

### 9.1 四层门控

1. 距上次成功整理至少 24 小时；
2. 文件系统门控扫描有节流，避免每轮重复遍历；
3. 自上次成功整理后至少完成 5 个 Agent turn；
4. 项目级 `.consolidate-lock` 不存在有效持有者。

锁文件 mtime 同时记录最近整理时间。进程崩溃后，超过 1 小时的锁视为过期，可安全接管。

### 9.2 整理职责

Dream 可以：

- 合并真正同义或互补的 Memory；
- 删除完全重复内容；
- 标记过期内容；
- 把过大的单文件拆成边界清晰的多个文件；
- 修复描述和索引；
- 保留冲突信息并使冲突可见。

Dream 不可以：

- 仅因文字相似合并不同流程；
- 抹掉仍可能有效的冲突分支；
- 生成当前项目中没有依据的新事实；
- 修改 `.incidentlens/memory/` 之外的文件；
- 删除审计、checkpoint、Session Memory 或工具输出。

Dream 使用同一原子写入协议。整理失败时保留整理前文件集合和原索引。

## 10. Session Memory

### 10.1 生成策略

Session Memory 优先从 `IncidentAgentState`、Evidence、已加载 Skill、预算、错误和 checkpoint 元数据确定性投影，不调用 LLM。用户约束从有界最近消息中提取；无法可靠结构化时保留原句的短引用，不进行推断扩写。

文件结构：

```markdown
# Session Memory

## Objective
调查 order-service 延迟告警。

## Verified facts
- Evidence ev-12: payment-service 下游 span 显著变慢。

## Rejected directions
- database_pool_exhaustion: contradicted by ev-15.

## Loaded skills
- downstream-timeout: /skills/downstream-timeout/SKILL.md

## Completed work
- 已检查慢 Trace 和完整调用链。

## Next action
- 核对 payment-service 同时间窗口日志。

## Constraints
- 仅允许只读调查工具。
```

### 10.2 更新时机

以下时机原子更新 Session Memory：

- 每个正常 stop；
- compact 前；
- 可恢复错误写入 checkpoint 后；
- 应用准备关闭且调查仍活跃时。

Session Memory 写入失败不能损坏 checkpoint；系统继续使用结构化状态，并记录快速压缩不可用原因。

## 11. 上下文压缩管线

压缩在每次模型调用前执行，顺序固定为工具结果预算、micro compact、消息裁剪、Session Memory 快速压缩、LLM 摘要兜底。

### 11.1 工具结果预算

触发条件：

- 单条工具结果超过 32KB；或
- 当前 turn 的工具结果总计超过 128KB。

处理：

- 完整内容写入 `.incidentlens/task-outputs/<incident-id>/<tool-call-id>.json`；
- 上下文保留最多 2KB 预览；
- 占位内容包含受控相对路径、SHA-256、原始字节数和重新读取说明；
- 从最大的结果开始持久化，直到当前 turn 回到预算内；
- 输出文件使用只允许当前服务进程读取的权限；
- 文件路径和内容都不能进入 Git。

工具输出持久化失败时，不立即丢弃原内容；继续后续压缩层。如果最终仍超限，进入可恢复错误路径。

### 11.2 Micro compact

- 保留最近 3 条完整 ToolMessage 结果；
- 更早且超过短内容阈值的结果替换为统一占位符；
- 已被工具结果预算持久化的引用保留；
- tool call 与 tool result 作为完整消息组处理；
- 未完成的 tool call 永不 micro compact；
- Evidence 已结构化进入状态，原 ToolMessage 被压缩不能删除 Evidence ID；
- Skill 文件内容可以被压缩，但 Session Memory 保留 Skill 名称和路径。

### 11.3 消息裁剪

消息数量或估算 token 超过软阈值时：

- 保留初始用户目标；
- 保留最近工作消息；
- 从中间最旧的完整消息组开始裁剪；
- 不拆分 `AIMessage(tool_call)` 与对应 `ToolMessage`；
- 不裁剪当前未完成调用；
- 不裁剪最后一次用户请求和最后一次模型响应；
- 在切口插入说明裁剪数量的有界占位消息。

精确 tokenizer 可用时使用精确 token；不可用时使用保守字符估算。估算误差只能让压缩更早触发，不能让系统在已知超限时继续无界调用。

### 11.4 Session Memory 快速压缩

当确定性层处理后仍接近模型阈值时，先检查 Session Memory 是否完整。完整性要求至少包含：

- Objective；
- Verified facts 或明确声明当前没有已验证事实；
- Loaded skills；
- Completed work；
- Next action；
- Constraints；
- 对所有当前结构化 Evidence ID 的覆盖或引用。

完整时，用以下集合替换旧历史：

- Session Memory；
- 当前结构化调查状态；
- 最近消息；
- 当前相关长期 Memory；
- 已加载 Skill 的恢复引用；
- 持久化工具输出引用。

这一路径不调用 LLM，是 IncidentLens 的默认全量压缩路径。

### 11.5 LLM 摘要兜底

只有 Session Memory 不完整，或快速压缩后仍超过阈值时调用摘要模型。

触发阈值：

```text
model_context_window - reserved_output_tokens - 13,000
```

`model_context_window` 和 `reserved_output_tokens` 来自模型配置；配置缺失时使用保守默认值，并在启动日志中标明。摘要输出上限必须小于保留输出预算。

摘要 prompt 首尾都明确要求：只返回文本，禁止调用任何工具。摘要必须覆盖：

1. 当前目标；
2. 用户约束；
3. 已完成工作；
4. 已读取或修改的位置；
5. 已验证事实和精确 Evidence ID；
6. 已排除方向与失败尝试；
7. 未完成工作和下一步；
8. 相关长期 Memory；
9. 已加载 Skill 与持久化输出引用。

摘要格式允许内部分析标签，但持久化和重新注入时只保留正式 summary，绝不保存隐藏推理。连续失败 3 次后摘要路径熔断，当前调查继续走响应式恢复或可恢复错误。

### 11.6 响应式恢复

模型返回 `prompt_too_long` 时：

1. 保存当前 checkpoint 与 Session Memory；
2. 按完整消息组从最旧部分继续裁剪；
3. 重新执行预算检查；
4. 最多重试两次。

当前目标、Session Memory、结构化 Evidence、最后用户请求和未完成 tool call 不能被响应式裁剪。两次后仍失败，返回明确的可恢复错误码，保留 checkpoint，后续 resume 不得从空状态重启。

### 11.7 Compact 后恢复顺序

恢复上下文的顺序固定为：

```text
Session Memory
  -> 当前调查结构化状态
  -> 最近消息
  -> 当前相关长期 Memory
  -> 已加载 Skill
  -> 持久化工具输出引用
```

同一内容只恢复一次。恢复总预算有硬上限；超限时优先保留结构化状态和 Session Memory，其次保留最近消息，最后按相关度裁剪长期 Memory 与工具输出预览。

## 12. 后台任务与并发

Memory side-query、提取和 Dream 由应用级有界任务管理器管理，不创建不可追踪的裸后台任务。

要求：

- 每类任务有独立超时；
- 同一项目最多一个 Memory 写事务；
- 同一项目最多一个 Dream；
- 队列满时先跳过低优先级 Dream，再跳过非关键重复提取；
- 主调查不等待提取和 Dream；
- 应用关闭时有限等待正在执行的原子写入，超时后安全取消未进入提交阶段的任务；
- 已进入原子替换提交阶段的任务不能被中途取消；
- 重复 stop hook 通过 turn 标识幂等去重。

后台失败记录错误分类、Memory 文件名、任务类型、耗时和降级结果，不记录原始敏感内容。

## 13. 路径与内容安全

- 所有路径解析后必须位于 `.incidentlens/` 对应子目录；
- 拒绝绝对路径、`..`、空 slug、控制字符和平台保留文件名；
- 拒绝通过符号链接逃逸项目目录；
- Memory 读取和写入都使用最大字节数限制；
- frontmatter 使用安全解析器，不实例化任意对象；
- 写入前扫描 API key、Token、Authorization、Cookie、私钥和常见云凭据模式；
- 日志与指标只记录计数、哈希或安全文件名；
- Memory 注入带不可信参考边界，不能成为 system 指令；
- Memory 与摘要不得包含隐藏推理；
- 工具输出文件权限限制为服务账户可读写。

## 14. RAG 退役与迁移

### 14.1 运行时删除范围

以下能力从运行时和产品表面彻底删除：

- Embedding provider 与 case embedding；
- SQLite FTS5 案例索引；
- hybrid/keyword case retrieval；
- 相似度、排序权重和降级模式；
- 历史案例召回、采用、validated/misleading 事件；
- 历史案例生成当前候选假设；
- 案例搜索、反馈、审核和治理 API；
- Web 案例治理与相似案例展示；
- RAG 对比评测策略与相关产品文案。

Agent system prompt 不再包含 Historical Cases 规则；`IncidentAgentState` 不再包含 `retrieved_cases`、`case_id` 和 `case_status`。运行时工厂不再注入 `InvestigationMemoryCoordinator` 或 `CaseRepository`。

### 14.2 数据库策略

- 新数据库不创建 RAG 案例、FTS、Embedding、反馈、审核和召回表；
- 已有数据库升级不执行 `DROP`；
- 新运行时不读取、不写入、不迁移遗留 RAG 表；
- schema 检测可以报告遗留表存在，但不能自动清除；
- 遗留表不影响应用启动和新 Memory 工作。

### 14.3 显式清理脚本

提供 `scripts/purge_legacy_rag.py`：

- 默认 dry-run，只列出会删除的已知 RAG 表和预计影响；
- 只有传入明确确认参数才执行；
- 执行前验证目标数据库路径，拒绝空路径、目录和非 SQLite 文件；
- 在事务内删除已知表和 FTS 辅助表；
- 不删除调查 checkpoint、审计、遥测、场景或评测运行数据；
- 输出删除结果与不可恢复警告。

该脚本不在部署启动、迁移或测试初始化中自动运行。

## 15. 配置

Memory 与 compact 使用显式配置对象，默认值如下：

- 长期 Memory 目录：`.incidentlens/memory`；
- Session Memory 目录：`.incidentlens/sessions`；
- 工具输出目录：`.incidentlens/task-outputs`；
- transcript 目录：`.incidentlens/transcripts`；
- Memory 扫描上限：200 文件；
- Memory 选择上限：5 文件/turn；
- Memory 单文件注入：200 行且 4KB；
- 单调查长期 Memory 注入：60KB；
- 索引上限：200 行且 25KB；
- 单工具结果持久化阈值：32KB；
- 单 turn 工具结果预算：128KB；
- 工具结果预览：2KB；
- micro compact 保留完整工具结果：3 条；
- auto compact 安全缓冲：13,000 token；
- 连续摘要失败熔断：3 次；
- prompt-too-long 响应式重试：2 次；
- Dream 最短间隔：24 小时；
- Dream 最少新 turn：5；
- Dream 锁过期：1 小时。

路径可由部署配置覆盖，但覆盖后的路径仍必须通过允许根目录校验。硬安全限制不能通过普通环境变量无限放大。

## 16. 可观测性

至少暴露以下安全指标和审计事件：

### 16.1 Memory

- 扫描、候选、选择和实际加载文件数；
- 注入字节数与单调查累计字节数；
- side-query 耗时、超时、解析失败和关键词降级；
- 提取新增、更新、跳过、冲突和脱敏计数；
- 原子写入失败、索引重建失败和非法文件计数；
- Dream 门控跳过原因、整理文件数、耗时和锁冲突。

### 16.2 Compact

- 每层触发次数；
- 每层处理前后消息数、字节数和估算 token；
- 工具输出落盘数量和字节数；
- Session Memory 快速压缩命中率；
- LLM 摘要调用率、耗时、失败和熔断；
- prompt-too-long 次数、恢复次数和最终可恢复失败；
- compact 后 Skill、Evidence 和 Memory 恢复数量。

指标不能包含 Memory 正文、用户消息、工具原始输出或凭据。

## 17. 错误处理与降级矩阵

| 故障 | 行为 | 主调查 |
|---|---|---|
| `MEMORY.md` 不存在 | 使用空索引并允许首次写入创建 | 继续 |
| 单个 Memory 无效 | 跳过并记录文件级错误 | 继续 |
| side-query 失败 | 关键词匹配 | 继续 |
| Memory 文件读取失败 | 跳过该文件 | 继续 |
| 提取失败 | 记录后台任务失败 | 不受影响 |
| Dream 失败 | 保留整理前文件和索引 | 不受影响 |
| Session Memory 写入失败 | 使用结构化 checkpoint | 继续，禁用快速压缩 |
| 工具输出持久化失败 | 保留原结果并继续后续层 | 继续或进入可恢复超限 |
| LLM 摘要失败 | 最多三次后熔断 | 走响应式恢复 |
| `prompt_too_long` | 最多两次完整消息组裁剪 | 失败后可恢复停止 |
| checkpoint 损坏 | 返回既有 checkpoint corruption 错误 | 绝不空状态重启 |
| 遗留 RAG 表存在 | 忽略并报告遗留状态 | 继续 |

## 18. 测试与验收

### 18.1 Memory 单元测试

- frontmatter 合法与非法输入；
- 四种类型校验；
- slug、绝对路径、`..` 和控制字符拒绝；
- 符号链接逃逸拒绝；
- 原子写入和失败回滚；
- 索引按稳定顺序重建；
- 200 行/25KB 索引上限；
- 200 文件扫描上限；
- 200 行/4KB 单文件注入上限；
- 60KB 单调查累计注入上限；
- side-query 最多 5 条且不能引用目录外文件；
- side-query 超时、异常和无效 JSON 的关键词降级；
- 提取无新内容返回空数组；
- 同义内容不重复增长；
- 冲突内容不静默覆盖；
- 凭据检测、脱敏和整条跳过；
- Dream 四层门控、锁过期和并发互斥。

### 18.2 Compact 单元测试

- 单条 32KB 和单 turn 128KB 边界；
- 落盘内容与 SHA-256 一致；
- 占位符包含受控路径、大小和预览；
- 最近 3 条工具结果保留；
- tool call/result 消息组不可拆分；
- 未完成 tool call 不被压缩；
- 消息裁剪保留初始目标和最近请求；
- Session Memory 完整性校验；
- 完整 Session Memory 路径不调用摘要模型；
- 摘要包含所有精确 Evidence ID；
- 连续 3 次摘要失败触发熔断；
- 两次响应式重试后返回可恢复错误；
- compact 前后 Objective、Constraints、Evidence、Skill 和 Next action 一致。

### 18.3 集成测试

- 每个正常 stop 后提交提取任务；
- 提取读取压缩前快照；
- 调查主路径不等待提取和 Dream；
- 多进程或并发 turn 不损坏 Memory 和索引；
- 一次调查跨 compact 后继续正确调用下一只读工具；
- 跨进程 resume 后恢复 Session Memory、Evidence 和 Skill；
- prompt-too-long 恢复不重复已成功工具调用；
- 新运行时不执行任何旧 RAG 表查询；
- 旧数据库升级后遗留表仍存在且无读写；
- 新数据库不创建 RAG 表；
- RAG 路由和页面入口不存在；
- dry-run 清理脚本不改变数据库；
- 显式清理只删除已知 RAG 表；
- Compose 中长期 Memory 可持久化并与 Git 目录一致。

### 18.4 项目质量门禁

- 全量 pytest 通过；
- Ruff lint 与 format check 通过；
- mypy 或项目现有类型检查通过；
- Compose 构建和集成场景通过；
- README、REQUIREMENTS、评测文档和演示文案不再声称支持 RAG；
- 静态搜索确认运行时代码不再引用 Embedding、HybridCaseRetriever、InvestigationMemoryCoordinator 或历史案例候选假设。

## 19. 组件与文件边界

实现计划应遵循以下责任拆分，具体文件名可在不破坏边界的前提下贴合现有包结构：

- `memory/domain`：Memory 元数据、候选和加载结果的纯领域类型；
- `memory/store`：安全路径、扫描、解析、原子写入和索引；
- `memory/selector`：side-query 与关键词降级；
- `memory/extractor`：stop 后候选提取、去重和敏感信息过滤；
- `memory/dream`：四层门控、锁与整理事务；
- `memory/runtime`：有界后台任务协调和 Agent 接入；
- `compaction/tool_budget`：大结果落盘与引用；
- `compaction/micro`：旧工具结果替换和消息组处理；
- `compaction/session`：Session Memory 投影、校验和恢复；
- `compaction/summary`：LLM 摘要和熔断；
- `compaction/middleware`：固定顺序编排、token 阈值和响应式恢复。

每个组件使用清晰接口通信。Memory store 不调用模型；selector 不写文件；extractor 不决定 compact；compaction 不创建长期 Memory；Agent middleware 不直接操作遗留 RAG 数据库。

## 20. 交付顺序

为保证每一步都可独立测试和回滚，实现按以下顺序进行：

1. 建立 Memory 领域类型、安全 store、索引和仓库目录；
2. 实现选择器、注入预算和确定性降级；
3. 实现 Session Memory 投影与恢复；
4. 实现工具结果预算、micro compact 和消息裁剪；
5. 实现 Session Memory 快速压缩、LLM 兜底和响应式恢复；
6. 实现 stop 后提取、有界任务管理器和 Dream；
7. 接入 Agent、checkpoint、配置和可观测性；
8. 从运行时移除 RAG 并执行非破坏性 schema 迁移；
9. 删除 RAG API/UI/评测语义，增加显式清理脚本；
10. 更新文档、Compose 和端到端验证。

任何阶段都不能通过让新 Memory 与旧 RAG 同时参与 Agent 推理来过渡。需要分阶段集成时，旧 RAG 必须先被 feature-disabled，且最终交付中删除该兼容开关。

## 21. 成功标准

重构成功必须同时满足：

- Agent 不再检索或使用历史案例 RAG；
- 新项目知识能在一个 turn 结束后自动写入 Git 管理的 Markdown Memory；
- 后续相关调查只加载少量相关 Memory；
- 无模型选择或提取能力时，主调查仍可降级运行；
- 长调查在上下文接近上限前优先通过确定性层释放空间；
- Session Memory 完整时无需调用摘要模型；
- compact 和 resume 不丢 Evidence ID、Skill、约束与下一步；
- 旧数据库数据不被普通升级删除；
- 新数据库和产品界面不再暴露 RAG；
- 全量质量门禁与 Compose 验收通过。
