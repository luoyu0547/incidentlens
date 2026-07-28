# IncidentLens 第三阶段：真实 LLM Agent、Skills 与稳固模型适配设计

## 目标

将第二阶段的确定性调查演示升级为真实的、有状态的 LLM Agent。Agent 必须通过
OpenAI-compatible 协议调用用户配置的 DeepSeek、GLM 或其他满足契约的模型，自主选择只读
工具、加载排障 Skill、形成和更新假设，并用当前事故的 Evidence ID 支撑根因报告。

本阶段的核心不是把已有规则包装成“Agent”，而是建立下面这条可验证链路：

```text
用户模型配置
    -> 真实模型请求
    -> 模型产生 tool_calls
    -> 只读工具查询当前遥测
    -> 工具结果形成 Evidence
    -> 模型更新调查假设
    -> 确定性证据门禁
    -> 根因报告
```

本阶段只有在真实模型契约测试和至少一个 Compose 级真实模型调查通过后，才可以声明完成。
缺少 API Key 时允许普通测试套件明确跳过 live 测试，但不得以 Fake Model、固定规则或静态结果
替代真实模型，并不得将这种状态描述为“真实 Agent 已完成”。

## 范围与非目标

范围：

- 使用 LangChain V1 的 Agent API 和 LangGraph V1 LTS 实现模型—工具调查循环。
- 使用统一模型配置在不修改业务代码的情况下切换 DeepSeek、GLM 等
  OpenAI-compatible 模型。
- 一次性交付标准 `SKILL.md` 目录、渐进式披露、引用资料读取、权限限制和恢复测试。
- 用 LangGraph SQLite checkpointer 保存每个模型步骤、工具步骤和调查状态。
- 保留第二阶段的证据归一化与报告守卫，作为 LLM 输出之外的确定性质量门禁。
- 为配置读取、工具调用、Skills、超时、重试、中断恢复和真实供应商调用建立自动化测试。

非目标：

- 不直接调用 OpenAI Python SDK，不手写 HTTP 请求、tool call 解析或 Agent 循环。
- 不使用旧版 `LLMChain`、`ConversationChain`、`langchain-classic` 或已弃用的
  `langgraph.prebuilt.create_react_agent`。
- 不同时引入 PydanticAI 作为第二套 Agent Runtime。
- 不将 LiteLLM Proxy 作为第三阶段必需基础设施。
- 不引入多 Agent、自动回滚、Shell、任意文件写入或生产系统写操作。
- 不承诺任意 OpenAI-compatible 文本模型都能运行；模型必须通过本项目的 tool-calling 契约。

## 技术栈与版本策略

第三阶段冻结下列生态边界：

| 组件 | 版本约束 | 锁定版本 | 职责 |
| --- | --- | --- | --- |
| `langchain` | `>=1.3,<2` | `1.3.14` | Agent API、统一模型接口、中间件 |
| `langgraph` | `>=1.2,<2` | `1.2.9` | 状态图、检查点、恢复、流式执行 |
| `langchain-openai` | `>=1.4,<2` | `1.4.1` | OpenAI-compatible Chat Completions 适配 |
| `langgraph-checkpoint-sqlite` | `>=3.1,<4` | `3.1.0` | 本地持久化 checkpointer |
| `deepagents` | `==0.6.12` | `0.6.12` | 仅使用 Skills 与受限文件中间件 |
| `pydantic` | `>=2.13,<3` | `2.13.4` | 配置、工具、证据和报告边界校验 |

依赖范围写入 `pyproject.toml`，实际解析版本由 `uv.lock` 固定。升级依赖只能通过显式更新
操作进行，并必须重跑模型适配、Skills 和恢复契约测试。

`deepagents` 当前仍是 pre-1.0，因此不得作为主 Agent Runtime。项目只允许通过内部
`SkillRuntime` 边界导入 `SkillsMiddleware`、受限 Filesystem middleware/backend 和权限
类型，不使用 `create_deep_agent`、Subagents、Todo、Memory Harness 或 Shell。这样其未来
API 变化只影响 Skills 适配层。

`openai` 可能作为 `langchain-openai` 的传递依赖出现，但 IncidentLens 源码不得导入它。
模型请求只通过 LangChain 的 `BaseChatModel`、`init_chat_model` 或 `ChatOpenAI` 发起。

## 总体架构

```text
config/models.yaml + secret env
              |
              v
        ModelRegistry
              |
              v
 LangChain create_agent ---------------- SkillsMiddleware
              |                                  |
              |                                  v
              |                     read-only /skills/** backend
              v
      LangGraph Agent Runtime
       |       |          |
       |       |          +---- SQLite checkpointer
       |       |
       |       +--------------- model/tool/skill audit events
       |
       v
 audited read-only telemetry tools
       |
       v
 Evidence normalization -> hypothesis update -> deterministic report gate
       |
       v
 investigation API / SSE / DemoRunner
```

LangChain `create_agent` 负责标准模型—工具循环，并在 LangGraph 上生成可检查的执行图。
LangGraph `thread_id` 固定使用 `incident_id`，因此 API、SSE、审计和 checkpoint 可以通过
同一 ID 关联。

## 状态和检查点

### 执行状态

LangGraph 状态成为 Agent 执行状态的唯一来源。自定义状态扩展 `AgentState`，至少包含：

```text
messages
incident_id
status
phase
alert
current_round
max_rounds
hypotheses
evidence
retrieved_cases
loaded_skill_names
model_profile
model_call_count
tool_call_count
report
```

LangChain V1 要求 Agent 自定义状态使用 `TypedDict`。现有 Pydantic
`InvestigationState` 继续作为领域校验、API 返回和数据库边界模型，不能直接作为
LangChain state schema。两者之间只有显式的校验投影，不维护两份可独立修改的状态。

现有自建 `CheckpointStore` 不再承担 Agent 执行恢复。迁移完成后：

- LangGraph SQLite checkpointer 保存每个模型和工具步骤；
- 现有 investigation/tool audit 表继续保存可查询审计记录；
- 现有 `InvestigationState` 由最新 LangGraph state 投影生成；
- 旧 checkpoint 表只允许迁移读取或在重置后停止使用，不能与 LangGraph 双写。

### 运行边界

- `start(alert)`：验证告警、创建 `incident_id/thread_id`、检索历史案例、生成初始候选并保存
  初始 checkpoint。
- `run_round(incident_id)`：恢复线程，执行一个有界调查回合；模型可以在该回合调用一个或
  多个只读工具，但受模型调用和工具调用预算限制。
- `resume(incident_id)`：从最后一个成功 LangGraph checkpoint 继续，不重新执行已成功步骤。
- `report_ready` 和 `needs_more_evidence` 是终止状态，后续调用不能静默重新运行。

所有外部调用和数据库副作用都必须位于可 checkpoint 的节点或工具中。工具仍保持只读和
幂等；同一个 `incident_id + tool_name + normalized_args` 使用稳定调用键去重。

## Agent 决策与确定性门禁

第三阶段删除 `_TOOL_STRATEGY` 固定顺序。模型根据告警、当前假设、证据摘要、历史案例和已
加载 Skill 选择下一项工具。

模型负责：

- 选择下一工具和参数；
- 说明该调用要验证的假设；
- 根据工具结果提出支持、反驳或新建假设；
- 在证据充分时提出结构化根因候选；
- 在证据不足、工具失败或结果矛盾时继续调查或明确停止。

模型不负责：

- 生成 Evidence ID；
- 声称工具已执行；
- 直接把历史案例当成当前根因；
- 绕过工具参数校验和只读权限；
- 单方面确认根因或决定报告通过。

每个真实工具返回继续经过现有 Pydantic `ToolResult` 校验，并由证据归一化层生成 Evidence。
现有 `evidence_rules.py` 从“决定工具顺序的规则”收缩为“将已执行结果转换为可验证事实”的
确定性逻辑。`can_generate_report` 继续要求：

1. 根因候选引用当前 incident 的 Evidence ID；
2. 支持性证据达到对应 Skill 定义的最低证据要求；
3. 没有未处理的直接反证；
4. 历史案例只能贡献先验候选，不能单独完成确认；
5. 报告中的服务、根因类型和 Evidence ID 通过 Pydantic 校验。

LLM 输出解析或校验失败时，最多进行一次结构化修复请求；修复失败后进入
`needs_more_evidence` 或明确的 `model_output_invalid`，不得回退到第二阶段固定结论。

## 模型配置与配置切换

### 配置文件

模型配置保存在 `config/models.yaml`，密钥只从环境变量读取：

```yaml
active_model: deepseek

models:
  deepseek:
    adapter: openai_compatible
    model: deepseek-chat
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2

  glm:
    adapter: openai_compatible
    model: glm-4.5
    base_url: https://open.bigmodel.cn/api/paas/v4
    api_key_env: GLM_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2

fallback_models: []
```

`INCIDENTLENS_MODELS_CONFIG` 可指定另一个配置文件，
`INCIDENTLENS_LLM_ACTIVE_MODEL` 可覆盖 `active_model`。环境变量优先于 YAML，但模型
名称、URL 和密钥来源必须在启动时完成一次 Pydantic 校验；未知字段、未知 profile、空
URL、缺少密钥或非法超时直接失败。该失败规则适用于 `llm_agent` 模式；
`deterministic_baseline` 不构造 ModelRegistry，因此可以继续运行不依赖外部模型的回归
评测。默认演示模式是 `llm_agent`。

切换 DeepSeek 和 GLM 只修改 `active_model` 或对应环境变量。Agent、工具、Skills 和
状态图不读取供应商名称，也不包含按模型名分支的业务逻辑。

### ModelRegistry

`ModelRegistry` 是唯一模型构造入口，返回 LangChain `BaseChatModel`。第一版只接受
`adapter: openai_compatible`，底层由 `ChatOpenAI` 使用 `model/base_url/api_key/timeout`
构造。新增原生 Anthropic、Gemini 或 LiteLLM 适配器时只能扩展 registry，不能修改 Agent
图。

所有可用 profile 必须满足共同契约：

- Chat Completions 消息语义；
- function/tool calling；
- 单工具和多轮工具结果；
- Pydantic 工具参数 JSON Schema 的兼容子集；
- 可获得模型名、延迟和 token usage（供应商提供时）；
- 超时和取消可传播到调用方。

为兼容不同国产模型，不依赖 provider-native structured output、Responses API、严格
JSON Schema、并行 tool calls 或供应商专有 reasoning 字段。结构化最终输出优先使用
LangChain `ToolStrategy`，再由 Pydantic 校验。

### Fallback

普通运行可以显式配置 `fallback_models`，并对网络错误、429 和 5xx 使用 LangChain
`ModelFallbackMiddleware`。默认列表为空，避免未声明的模型替代；一旦配置 fallback，所有
被引用 profile 都必须在启动时通过完整配置校验。以下错误不得 fallback：

- 401/403；
- profile 配置错误；
- 模型不支持工具调用；
- 工具参数验证失败；
- 证据不足；
- Agent 业务门禁拒绝报告。

真实供应商契约测试强制关闭 fallback，以证明指定 profile 本身可用，而不是由其他模型
替它通过。

## 超时、重试和预算

超时分层：

| 层次 | 默认值 | 行为 |
| --- | --- | --- |
| 模型连接 | 15 秒 | 建连失败后进入模型传输重试 |
| 模型读取 | 300 秒 | 允许推理模型长时间返回 |
| 单工具 | 现有 3 秒 | 工具内部最多重试一次并返回 `ToolResult` |
| 单调查总预算 | 1200 秒 | 超时保存 checkpoint，状态为可恢复失败 |
| 最大模型调用 | 12 | 超出后停止并说明预算耗尽 |
| 最大工具调用 | 12 | 超出后停止并保留已有证据 |
| 最大调查回合 | 8 | 保持现有 API 语义 |

模型传输重试最多两次，只重试连接错误、429 和 5xx，并使用指数退避和 `Retry-After`。
401、403、404、模型不支持工具和请求格式错误立即失败。不得同时在底层客户端、
`ModelRetryMiddleware` 和业务循环重复配置同一类重试；模型 registry 是传输重试的唯一
责任层。

模型超时不会触发规则引擎或 Fake Model fallback。超时、重试次数、最后错误、profile 和
checkpoint ID 都进入审计，但 API Key、完整 Authorization 头和未脱敏请求内容不得进入
日志。

## Skills

### 目录与范围

本阶段一次性交付五个与当前可复现故障一一对应的 Skill：

```text
skills/
├── downstream-timeout/
│   ├── SKILL.md
│   ├── evidence-policy.yaml
│   └── references/
├── downstream-error/
│   ├── SKILL.md
│   ├── evidence-policy.yaml
│   └── references/
├── database-pool-exhaustion/
│   ├── SKILL.md
│   ├── evidence-policy.yaml
│   └── references/
├── dependency-unavailable/
│   ├── SKILL.md
│   ├── evidence-policy.yaml
│   └── references/
└── deployment-regression/
    ├── SKILL.md
    ├── evidence-policy.yaml
    └── references/
```

每个 `SKILL.md` 使用 Agent Skills frontmatter，至少包含：

```yaml
name:
description:
license:
compatibility:
metadata:
  version:
allowed-tools:
```

正文必须定义适用症状、推荐调查顺序、需要验证的候选假设、最低支持证据、反证、停止条件、
禁止行为和引用资料。`references/` 保存查询说明、证据解释或版本化 runbook；大段资料不得
全部塞入 `SKILL.md`。`evidence-policy.yaml` 使用 Pydantic 校验的机器可读结构声明
`cause_code`、必需证据类型、最低独立证据数和直接反证；确定性报告门禁读取该文件，不能
从 `SKILL.md` 自然语言正文猜测验收规则。

### 加载和权限

`SkillRuntime` 使用 Deep Agents 的 `SkillsMiddleware` 完成 frontmatter 扫描和渐进式
披露。启动时模型只看到 Skill 名称、description 和路径；匹配后通过受限 `read_file`
读取完整 `SKILL.md`，再按需读取 references。IncidentLens 不实现另一套 Skill 选择协议。

Skills 文件访问使用 `FilesystemBackend(virtual_mode=True)`，根目录固定为项目 `skills/`，
并配置 first-match-wins 权限：

1. 允许读取 `/skills/**`；
2. 拒绝读取其他路径；
3. 拒绝所有写操作。

不提供 Shell 或 `execute`。Skill 中如需确定性计算，必须将逻辑注册为经过 Pydantic 参数
校验和审计的显式只读工具，不能直接执行 Skill 附带脚本。读取 Skill 和 reference 同样
产生审计事件。包含路径穿越、重复 name、超长 description、未知 `allowed-tools` 或缺少
必填 frontmatter 的 Skill 会使应用启动失败。

`deepagents==0.6.12` 的所有导入集中在 `SkillRuntime` 模块。契约测试固定其 metadata
结构、权限行为和 prompt 渐进式披露；升级 Deep Agents 前必须先让这些测试在新版本通过。

## 历史案例与 RAG

现有历史案例检索在模型第一次行动前执行，并向 Agent 提供：

- 相似案例 ID 和相似度；
- 历史症状、根因类别和服务；
- 已验证证据摘要；
- 适用版本和状态；
- 解决方案与无效假设。

只检索 `human_verified` 或测试夹具明确标记为可信的案例。历史案例生成低置信候选假设，
Agent 必须用当前日志、指标、Trace 和部署记录重新验证。报告需区分：

- 当前遥测证据；
- 历史案例先验；
- 被当前证据推翻的历史类比。

RAG 命中不能提高到可确认阈值，也不能绕过 Evidence ID 门禁。

## 审计与可观测性

每次调查至少记录：

- incident/thread/checkpoint ID；
- 配置 profile、模型名和脱敏 endpoint host；
- 模型调用开始、结束、耗时、重试、token usage；
- 模型返回的标准化 tool call 名称和参数摘要；
- 工具实际执行及 `ToolResult`；
- Skill metadata 扫描、`SKILL.md` 和 reference 读取；
- Evidence 创建及假设状态变化；
- fallback、超时、取消和恢复；
- 报告守卫接受或拒绝的原因。

不保存模型隐藏推理。消息内容如持久化，必须限制大小，并在日志视图中对 API Key、认证头和
配置密钥进行脱敏。SSE 只发送状态迁移、工具、Skill、Evidence 和报告事件，不发送 secrets
或框架内部对象。

## 测试策略

### 单元测试

单元测试不访问网络，并全局禁止意外真实模型请求。覆盖：

- YAML、环境变量覆盖和密钥来源解析；
- 未配置、部分配置、非法 URL、未知 profile 的显式失败；
- ModelRegistry 将 sentinel model、base URL、超时和重试传给 LangChain 模型；
- DeepSeek/GLM 切换不修改 Agent 构造；
- 模型输出、工具参数和报告的 Pydantic 校验；
- fallback 只接受允许的传输错误；
- 预算和终止条件。

Fake ChatModel 只用于单元和图集成测试，并必须通过依赖注入显式传入。生产构造路径不存在
“未配置则自动 Fake”的分支。

### Skills 契约测试

覆盖：

- 五个 Skill 全部发现且 frontmatter 合法；
- 重复 name、未知工具、缺失文件和路径穿越启动失败；
- 初始 prompt 只包含 description，不包含完整正文；
- 选择 Skill 后读取完整 `SKILL.md` 和指定 reference；
- `/skills/**` 可读，其他路径和所有写入被拒绝；
- checkpoint 恢复后保留已发现 Skills 和读取审计；
- 不加载无关 Skill。

### LangGraph 恢复测试

使用 Fake ChatModel 和真实 SQLite checkpointer：

1. 模型产生 tool call；
2. 工具成功并形成 Evidence；
3. 在下一模型步骤前注入中断；
4. 用同一 `incident_id/thread_id` 恢复；
5. 断言已成功工具不重复执行、Evidence ID 不变化；
6. 调查继续到报告或明确终止。

### 真实供应商契约测试

`pytest -m live_llm` 才运行真实模型，且每个测试关闭 fallback。若目标 profile 的密钥完全
缺失，测试明确 `SKIP` 并显示缺少的环境变量；配置文件存在但缺字段、密钥为空、URL 非法或
认证失败时必须 `FAIL`。

每个 profile 的 canary 测试：

1. 从与生产相同的 `ModelRegistry` 读取配置；
2. 生成随机 nonce；
3. 将只读 canary tool 绑定为必需工具；
4. 向真实模型发出请求；
5. 断言返回标准化 tool call；
6. 执行 tool 并断言审计记录包含 nonce；
7. 断言模型名和脱敏 endpoint 与所选 profile 一致。

这证明测试确实读取了用户配置、连接了目标 endpoint，并发生了真实工具调用。

### 真实 Agent Compose 验收

至少一个场景使用真实配置模型执行完整 Compose 调查，并断言：

- Agent trace 中存在真实模型响应；
- 至少一次工具由模型选择，而非固定 `_TOOL_STRATEGY`；
- 模型实际读取了适用 Skill；
- 当前遥测生成 Evidence；
- 报告引用的每个 Evidence ID 属于当前 incident；
- 根因服务符合场景验收；
- API、SSE、CLI 和审计没有密钥；
- 无 Fake Model、固定答案或 fallback 模型替代。

随后五个场景都应加入可重复 live 评测；模型具有非确定性，因此业务正确性以证据门禁和场景
结果衡量，不使用精确文本快照。

## 错误处理

- 模型不可用：保存 checkpoint，返回 `model_unavailable` 和可恢复状态。
- 模型超时：保存已完成工具和 Evidence，返回 `model_timeout`，允许 resume。
- 模型无 tool call：若证据不足则继续一次提示修复；仍无行动则
  `needs_more_evidence`，不得自动选择固定工具。
- 非法工具参数：Pydantic 拒绝并将可修复错误反馈模型一次。
- 重复工具调用：返回已有调用摘要和 Evidence ID，不重新查询。
- Skill 无法读取：记录 `skill_load_failed`；若该 Skill 是当前结论所需则禁止生成报告。
- 工具失败或空结果：形成失败/空结果 Evidence，供 Agent 改变假设，不伪装成功。
- fallback 全部失败：保存所有脱敏错误摘要并终止本回合。
- checkpoint 损坏：明确失败并保留原始记录，不从空状态重新调查。

## 迁移顺序

实现必须按照下列边界迁移，任何中间状态都不能宣称第三阶段完成：

1. 加入依赖锁和 ModelRegistry，但不替换现有调查路径；
2. 完成配置、canary 和 Fake Model 测试；
3. 建立 LangChain/LangGraph Agent state 和 SQLite checkpointer；
4. 接入现有七个只读工具及 Evidence middleware；
5. 一次性交付五个 Skills、受限 backend 和契约测试；
6. 将 `_TOOL_STRATEGY` 路径替换为真实 Agent 决策；
7. 迁移 API、SSE 和 DemoRunner 到 LangGraph state；
8. 通过中断恢复、无 Fake fallback 和 Compose 测试；
9. 使用用户配置的真实模型通过 canary 与至少一个完整场景；
10. 扩展到五场景真实评测并记录失败分析。

迁移期间可以保留第二阶段规则路径作为测试基线，但生产调查入口不得根据 API Key 是否存在
自动选择规则路径。运行模式必须显式：

```text
deterministic_baseline
llm_agent
```

默认演示和第三阶段验收使用 `llm_agent`。基线模式只能用于评测对照，并在 API、CLI 和报告
中清楚标识。

## 完成标准

第三阶段同时满足下面两组条件才算完成。

### 代码完成

- LangChain/Graph/Skills 依赖按本设计锁定，未使用旧版或已弃用 API；
- `_TOOL_STRATEGY` 不再控制 `llm_agent`；
- 模型只通过配置选择，源码不直接导入 OpenAI SDK；
- 五个 Skills、references、权限和渐进式披露一次性交付；
- LangGraph checkpoint 能从模型/工具步骤中断后恢复；
- 单元、Skills、图集成、现有五场景确定性回归、ruff 和 mypy 全部通过；
- 缺少或错误模型配置不会静默 fallback。

### 真实供应商验证

- 用户指定 profile 的 canary tool-calling 测试通过；
- 测试输出证明使用了所选模型和脱敏 endpoint；
- 至少一个 Compose 场景由真实模型完成工具调查、Skill 加载、Evidence 和报告闭环；
- 真实运行未使用 Fake Model、固定答案或其他 profile fallback；
- 超时、重试和 20 分钟总预算按配置生效。

如果只满足“代码完成”而没有真实供应商验证，状态必须报告为：

```text
Agent implementation complete; live provider verification pending.
```

不能报告为“第三阶段完成”或“真实 Agent 已完成”。

## 调研依据

本设计于 2026-07-28 依据以下官方资料冻结：

- [LangChain V1 与当前 Agent API](https://docs.langchain.com/oss/python/releases/langchain-v1)
- [LangChain 统一模型接口与 OpenAI-compatible endpoint](https://docs.langchain.com/oss/python/concepts/providers-and-models)
- [LangGraph V1 稳定性与 LTS 策略](https://docs.langchain.com/oss/python/releases/langgraph-v1)
- [LangGraph persistence 与 SQLite/Postgres checkpointer](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Deep Agents Skills 与 Agent Skills 目录约定](https://docs.langchain.com/oss/python/deepagents/skills)
- [Deep Agents 版本状态](https://docs.langchain.com/oss/python/versioning)
- [Deep Agents 文件权限](https://docs.langchain.com/oss/python/deepagents/permissions)
- [LiteLLM 统一网关、路由与 fallback](https://docs.litellm.ai/)
