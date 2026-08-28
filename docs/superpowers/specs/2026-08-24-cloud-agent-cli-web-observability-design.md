# IncidentLens 云端 Agent CLI 与日志观察 Web 设计

**日期：** 2026-08-24
**状态：** 已确认（2026-08-24）
**范围：** 新 CLI、新 Web，以及支撑二者的后端产品接口
**替代关系：** 本设计取代已删除的 `2026-08-13-cli-web-reports-phase-5-design.md`，并取代 `2026-08-21-hard-cloud-incident-terminal-design.md` 中以 `incidentlens run ...` 和旧 Textual 多面板 TUI 为主的产品交互；后者的远程安全、证据、审批、变更和验收约束继续有效。

## 1. 背景

IncidentLens 已有一套 Python/FastAPI 云端调查控制面。当前后端包含 Investigation/Agent 状态机、SSH 和远程工具、日志采集与脱敏、Evidence、Approval、ChangeSet、恢复和 durable events。当前工作树已经删除旧 Jinja2/HTMX Web 与 Textual CLI，因此本设计不受旧界面兼容约束。

产品需要两个职责不同但共享同一后端状态的界面：

- **CLI 是执行和控制面。** 用户像使用 Claude Code 一样运行 `incidentlens`，通过自然语言要求 Agent 调查远程云主机，并在终端完成危险操作审批。
- **Web 是观察面。** 用户独立打开网页，清晰查看真实云端日志、服务状态、当前问题、调查发现、修复和验证结果。Web 的存在是因为终端不适合高密度日志、复杂筛选、堆栈展开、趋势和多视图关联。

CLI 与 Web 不互相跳转，不依赖彼此提供链接，也不复制彼此的核心交互。

## 2. 产品原则

### 2.1 一句话定义

> 用户在 CLI 中与 Agent 一起操作云端，在 Web 中持续、直观地看清云端。

### 2.2 核心原则

1. 用户不需要先在 Web 创建“项目”才能调查。
2. 本地源码是可选辅助上下文；即使本地没有源码，Agent 也必须能依靠 SSH、容器、远程文件和日志完成调查。
3. 用户只需配置远程目标的名称、主机、用户名、端口和认证引用；Agent 负责发现远程服务和调查入口。
4. 自然语言是 CLI 主流程；`/` 指令只管理确定性的系统状态。
5. CLI 不承担完整日志浏览器职责；Web 不提供 Agent 对话、远程执行或审批入口。
6. FastAPI Control Plane 是唯一执行、安全和审计边界。
7. 浏览器与 CLI 都不直接连接 SSH、不直接操作 SQLite、不直接调用模型或远程工具网关。
8. 所有客户端可见日志和证据在服务端完成敏感信息脱敏，但保留真实内容、结构、顺序和上下文。
9. CLI 断开、Web 刷新或网络抖动不得改变 Agent 任务的权威状态。
10. 危险工具调用的审批、意图哈希、有效期、单次消费、执行前复核和审计始终由服务端保证。

## 3. 已确认的总体架构

```text
                         ┌────────────────────────┐
                         │ IncidentLens CLI       │
                         │ TypeScript + React Ink │
                         │ 对话 / 控制 / 审批     │
                         └───────────┬────────────┘
                                     │ HTTP + WebSocket
                                     ▼
┌────────────────────────┐   ┌─────────────────────────┐
│ IncidentLens Web       │   │ FastAPI Control Plane   │
│ Vite + React           │◀──│ Agent Runtime           │
│ 日志 / 状态 / 结果     │   │ 策略 / 状态机 / 审计   │
└───────────┬────────────┘   └────────────┬────────────┘
            │ HTTP + SSE/WS               │ SSH / Docker / Files
            └─────────────────────────────┤
                                          ▼
                                云端主机与微服务
```

### 3.1 中心服务模式

Agent Runtime、模型调用、SSH、工具执行、审批策略、备份、回滚和持久化都运行在中心服务。CLI 与 Web 是客户端。

该模式保证：

- CLI 退出不隐式取消调查；
- 操作者可以重连并恢复会话；
- Web 能持续观察同一份权威日志和调查状态；
- 模型和 SSH 凭据不散落在每个 CLI 安装中；
- 安全策略、审批和审计只有一套实现；
- 现有 Python Investigation Runtime 可直接复用。

第一版不实现 CLI 本地执行器。本地源码支持作为未来扩展保留，但不能成为远程调查的依赖。

## 4. CLI 产品设计

### 4.1 技术选型

CLI 使用：

- TypeScript；
- React；
- Ink；
- Ink Testing Library；
- HTTP API client；
- WebSocket 实时事件客户端；
- 真实 PTY 端到端测试。

选择 Ink 的原因：

- 产品是持续对话、流式回复、工具状态和审批卡片，而不是全屏 Dashboard；
- React 组件模型适合消息、输入、命令面板、工具卡片、审批和状态栏；
- UI 迭代速度高于 Rust/Ratatui；
- 比 Python Textual 更不容易重新发展为多窗口运维面板；
- CLI 与 Web 可以共享纯 TypeScript 的协议类型、格式化工具和测试夹具，但不共享 UI 组件。

不选择：

- **Textual：** 更适合固定布局、多屏和数据面板；完整日志展示已经属于 Web。
- **Ratatui：** 发布和性能优秀，但当前产品仍在快速迭代，基础交互开发成本过高。
- **手写 ANSI：** 光标、resize、输入和局部更新可靠性成本不值得承担。

### 4.2 启动体验

主入口只有：

```bash
incidentlens
```

进入后：

```text
IncidentLens

未选择目标
输入 /help 查看指令，或直接描述要调查的问题。

> _
```

普通文本发送给 Agent；以 `/` 开头的输入由确定性的指令系统处理。

### 4.3 自然语言主流程

```text
> /target production
✓ 已连接 production

> 调查 payment-service 为什么频繁重启

● 正在检查容器状态
● 已定位重启时间窗口
● 已采集相关日志
◐ 正在验证数据库连接池假设

> 同时检查最近是否修改过连接配置

● 已加入调查约束
◐ 正在比较运行配置与已有证据
```

用户发送第一条调查请求时，服务端自动创建 Agent Session、Investigation 和内部 AgentRun。CLI 不要求用户理解或手工编排这些内部资源。

### 4.4 CLI 展示边界

CLI 展示：

- Agent 的文本回复；
- 当前调查步骤和简短进度；
- 工具开始、完成、失败和安全摘要；
- Todo、假设和关键发现摘要；
- 子任务状态；
- 等待审批、暂停、取消和完成；
- 精确的审批卡片与安全 diff；
- 网络断线和恢复状态。

CLI 不展示：

- 无界实时日志；
- 复杂日志筛选和趋势图；
- Web URL 或“打开浏览器”操作；
- 隐藏思维链；
- 原始凭据、未脱敏输出或内部 provider payload；
- 传统资源管理 Dashboard。

### 4.5 `/` 指令系统

输入 `/` 打开可搜索命令面板；输入部分名称即时过滤，支持上下键、Enter、Tab 和 Esc。危险命令必须二次确认。

第一版命令：

| 分组 | 指令 | 作用 |
| --- | --- | --- |
| 帮助 | `/help` | 指令与快捷键 |
| 帮助 | `/status` | 中心服务、模型、目标和当前调查状态 |
| 帮助 | `/doctor` | 网络、认证、SSH、Docker 和模型能力检查 |
| 目标 | `/target` | 查看和选择目标 |
| 目标 | `/target add` | 交互式添加远程目标 |
| 目标 | `/target edit` | 编辑当前目标 |
| 目标 | `/target test` | 测试 SSH 和远程能力 |
| 目标 | `/target remove` | 删除目标，二次确认 |
| 连接 | `/disconnect` | 断开当前远程会话 |
| 连接 | `/reconnect` | 重连当前目标 |
| 会话 | `/new` | 开始新的调查上下文 |
| 会话 | `/sessions` | 查看和切换历史会话 |
| 会话 | `/resume` | 恢复最近未完成会话 |
| 会话 | `/rename` | 修改会话标题 |
| 会话 | `/clear` | 清除终端显示，不删除服务端记录 |
| 会话 | `/compact` | 请求压缩 Agent 上下文 |
| 会话 | `/cancel` | 显式请求取消当前操作 |
| 范围 | `/services` | 查看远程发现的服务 |
| 范围 | `/service` | 选择当前重点服务 |
| 范围 | `/scope` | 查看或收紧 Agent 范围 |
| 调查 | `/plan` | 查看调查计划 |
| 调查 | `/todos` | 查看步骤状态 |
| 调查 | `/evidence` | 查看证据摘要 |
| 调查 | `/hypotheses` | 查看假设与置信度 |
| 审批 | `/approvals` | 查看待处理审批 |
| 审批 | `/approve` | 批准当前审批 |
| 审批 | `/reject` | 拒绝当前审批 |
| 审批 | `/diff` | 查看安全变更预览 |
| 系统 | `/model` | 查看或选择服务端允许的模型 |
| 系统 | `/exit` | 退出 CLI，不隐式取消服务端调查 |

指令分两类：

- **纯客户端：** `/help`、`/clear`、`/exit` 等；
- **服务端指令：** `/target`、`/sessions`、`/cancel`、审批和模型配置等。

自然语言始终发送给 Agent Session。CLI 不根据文本自行选择 SSH 或远程工具。

### 4.6 目标配置

目标通过进入 CLI 后的 `/target` 指令管理，不以 Web 项目创建流程为前提。

```text
> /target add

添加远程目标
名称       production
主机地址   10.0.1.20
SSH 用户   deploy
SSH 端口   22
认证方式   SSH Agent
```

目标的产品模型：

```text
Target
├── target_id
├── name
├── host
├── ssh_user
├── ssh_port
├── authentication_ref
├── host_key_policy
├── optional_source_path
└── discovered_capabilities/services
```

凭据明文不通过普通读取 API 返回。实际 SSH 由中心服务发起。第一版可以把 Target 映射到现有 Project Registry 的内部 project/target/service 约束，避免重写既有 scope 和安全策略，但 CLI 用户不必理解“先建项目”的工作流。

服务由 Agent 在授权边界内从远程 Docker/Compose、文件和日志入口发现。发现新的容器或路径不会自动扩大权限；需要沿用现有 Registry Proposal 与审批机制。

### 4.7 审批体验

危险操作在对话流中显示审批卡片：

```text
需要审批

Agent 准备修改：
  /opt/payment/config.yaml

  pool_size: 20
  pool_size: 50

影响：修改配置后重启 payment-service
验证：检查健康状态并重放失败请求
回滚：恢复已验证备份并重启原服务

[A] 批准一次  [R] 拒绝  [D] 查看完整差异
```

必须保留的服务端约束：

- 精确 canonical intent 与哈希；
- 审批有效期；
- 单次消费；
- 认证操作者身份和理由；
- 批准后、执行前再次验证目标、参数、scope 和当前状态；
- 取消后到达的审批不得执行；
- 不确定远程结果不得自动重放。

### 4.8 CLI 发布

开发和首个稳定版本优先使用 npm：

```bash
npm install -g @incidentlens/cli
incidentlens
```

之后评估 Bun `--compile` 生成 macOS、Linux 和 Windows 平台二进制，并保留 npm 回退。不得仅凭编译成功宣称可发布；必须在真实 PTY 和目标 OS 上验证 Ink 渲染、中文宽字符、resize、Ctrl+C、WebSocket 恢复、配置持久化和凭据存储。

不采用已废弃的 `pkg`。Node SEA 仅作为 Bun 与 Ink 兼容性不满足时的候选，并需接受其模块加载和活跃开发限制。

## 5. Web 产品设计

### 5.1 技术选型

Web 使用：

- React + TypeScript；
- Vite；
- TanStack Router；
- TanStack Query；
- TanStack Table；
- TanStack Virtual；
- Apache ECharts；
- 从 FastAPI OpenAPI 生成的 TypeScript client。

不选择 Next.js：IncidentLens 是登录后的运维工作台，不依赖 SEO、请求时 SSR 或 Server Components。Next.js 会增加 Node 生产运行时、反向代理、缓存和双服务部署复杂度；静态导出时又无法带来足够收益。

不选择 Jinja2 + HTMX 作为主路线：它适合低交互 CRUD MVP，但本产品的核心价值是大量日志虚拟化、结构化展开、实时流、复杂筛选和跨视图联动。当前旧 HTMX 页面也已从工作树删除，不存在渐进迁移收益。

### 5.2 Web 定位

Web 是长期运行、独立访问的云端观察工作台。用户自行选择环境、目标、主机或服务。Web 不依赖 CLI 链接或当前终端会话。

Web 回答：

> 云端现在发生了什么、影响了什么、证据是什么、Agent 找到了什么、最后如何解决。

### 5.3 页面信息架构

```text
总览
├── 目标与主机状态
├── 服务状态
├── 当前问题
└── 最近解决结果

服务
├── 健康状态
├── 容器、版本与重启信息
├── 实时日志
├── 历史日志搜索
├── 已发现问题
└── 相关调查与修复

问题
├── 症状和影响
├── 根因与置信度
├── 关联证据
├── 解决措施
└── 验证结果

调查
├── 状态与里程碑
├── 假设演进
├── 证据时间线
├── Agent 行动摘要
└── 等待 CLI 审批状态
```

Web 不提供：

- Agent 对话输入；
- 工具执行按钮；
- approve/reject；
- 远程 shell；
- 文件修改、服务重启或回滚；
- Agent 内部上下文、隐藏推理和调试 payload；
- 以“新建项目”为主的产品入口。

目标编辑和安全配置由 CLI `/target` 指令负责；Web 只读取安全摘要。

### 5.4 日志页

日志页是第一优先级和产品中心，必须支持：

- 目标、主机、服务、容器实例、级别、时间范围筛选；
- 实时模式和历史搜索模式；
- cursor 分页和虚拟滚动；
- 自动滚动、暂停、继续和定位最新；
- 多行异常堆栈折叠；
- JSON 日志结构化展开；
- 关键词高亮；
- 某条日志前后上下文；
- Agent 已采纳证据的明确标记；
- 从问题、假设、根因和验证结果定位到对应日志；
- 修复前后日志对比；
- 日志速率、ERROR/WARN 趋势；
- 复制和允许范围内的脱敏导出；
- 断线后从 cursor 补齐，再切回实时流。

“真实日志”表示忠实保留服务端采集到的内容、时间、级别、结构和顺序，但密钥、Token、密码和策略禁止字段必须先在服务端脱敏。浏览器不得获得原始未脱敏日志。

### 5.5 调查与日志联动

Web 必须解释 Agent 为什么关注某些日志：

- Agent 当前观察的时间区间和筛选；
- 哪些日志被采纳为 Evidence；
- 每个假设由哪些日志支持或反驳；
- 根因结论引用哪些证据；
- 修改前后日志和服务行为如何变化。

点击 Evidence、Hypothesis、Conclusion 或 Verification 时，日志视图应定位到对应时间和上下文。事件只传安全 ID 和摘要；大对象由 Web 再通过 HTTP 获取权威资源。

### 5.6 面向用户的 Issue 投影

Web 使用稳定、只读的 Issue 聚合，而不是直接暴露所有内部 run：

```text
Issue
├── issue_id
├── target/service
├── title
├── severity
├── status
├── symptom
├── root_cause
├── confidence
├── evidence
├── resolution
├── verification
└── timestamps
```

第一阶段由现有 Investigation、Conclusion、Evidence 和 ChangeSet 聚合生成。Issue 是面向展示的投影，不应复制或绕过 Investigation 的权威状态机。

## 6. 后端产品接口层

### 6.1 原则

不重写成熟的 Investigation、Evidence、Approval、ChangeSet、Log 和 Recovery 核心。新增面向 CLI/Web 的 facade：

1. Resource API；
2. durable Operation API；
3. versioned Stream API；
4. Web 只读聚合 API；
5. authenticated principal 与统一错误契约。

客户端不得直接围绕内部 store 或 `RemoteToolGateway` 编排业务。

### 6.2 Agent Session

CLI 面向持续对话 Session：

```text
AgentSession
├── session_id
├── title
├── target_id
├── active_investigation_id
├── status
├── created_at
├── updated_at
└── last_event_sequence
```

建议接口：

```text
POST   /api/v1/agent-sessions
GET    /api/v1/agent-sessions
GET    /api/v1/agent-sessions/{session_id}
PATCH  /api/v1/agent-sessions/{session_id}
POST   /api/v1/agent-sessions/{session_id}/messages
GET    /api/v1/agent-sessions/{session_id}/messages
POST   /api/v1/agent-sessions/{session_id}/cancel
POST   /api/v1/agent-sessions/{session_id}/resume
```

发送消息后立即返回：

```json
{
  "message_id": "msg_01...",
  "operation_id": "op_01...",
  "accepted": true
}
```

Session facade 负责内部 Investigation/Run 创建、恢复和关联；CLI 不手工编排底层资源。

### 6.3 Durable Operation

长任务统一建模：

```text
Operation
├── operation_id
├── kind
├── status
├── target_id
├── session_id
├── investigation_id
├── progress_summary
├── error_code
├── created_at
└── finished_at
```

适用范围：

- Agent 消息和调查运行；
- Target 连接/能力测试；
- rollback；
- 报告生成；
- 其他不可在短 HTTP 请求内可靠完成的操作。

接口：

```text
GET  /api/v1/operations/{operation_id}
POST /api/v1/operations/{operation_id}/cancel
```

关键操作不能依赖 FastAPI `BackgroundTasks` 或仅存在于进程内的 task 作为唯一状态。Operation 必须可恢复、可审计，并与既有 Investigation 状态机一致。

### 6.4 Target facade

```text
POST   /api/v1/targets
GET    /api/v1/targets
GET    /api/v1/targets/{target_id}
PATCH  /api/v1/targets/{target_id}
DELETE /api/v1/targets/{target_id}
POST   /api/v1/targets/{target_id}/test
GET    /api/v1/targets/{target_id}/services
```

Target API 是现有 Project Registry 的产品 facade。它不得降低：

- target/service scope；
- canonical path；
- protected path；
- SSH host identity；
- Registry Proposal；
- optimistic concurrency；
- credential secrecy。

### 6.5 Web 聚合 API

建议第一版：

```text
GET /api/v1/overview
GET /api/v1/targets
GET /api/v1/targets/{id}/services
GET /api/v1/services/{id}
GET /api/v1/services/{id}/logs
GET /api/v1/services/{id}/issues
GET /api/v1/issues
GET /api/v1/issues/{id}
GET /api/v1/investigations
GET /api/v1/investigations/{id}/summary
GET /api/v1/evidence/{id}
```

所有正式 endpoint 必须声明 Pydantic response model。OpenAPI 是客户端类型生成的唯一事实源，不允许 Web 和 CLI 手写重复 DTO。

### 6.6 Approval facade

现有 Approval API 需要补充：

- 单条详情；
- session/investigation/run/tool/changeset 关联；
- 风险、到期时间和安全预览；
- authenticated actor；
- approve/reject reason；
- “决策已持久化”和“下游已处理”两个状态；
- 分页和过滤。

Web 只读展示等待状态和最终决策，不调用决策接口。CLI 是交互式审批入口，但安全授权仍由服务端实施。

### 6.7 Report 与 Project Memory

现有内部 Report 和 Project Memory 能力后续通过只读 facade 暴露。Report 内容不能以服务器本地路径作为客户端资源；应有持久 report record、版本、状态和受控内容/下载接口。Project Memory 默认只读展示来源，不允许客户端无证据任意写入。

## 7. 通信协议

### 7.1 CLI HTTP

HTTP 用于确定性操作：

- Session 创建和消息提交；
- `/target` 指令；
- status/doctor；
- cancel/resume；
- approval decision；
- 历史和快照读取。

所有 mutation 支持明确 idempotency 语义。统一错误格式：

```json
{
  "code": "target_unreachable",
  "message": "Unable to connect to the selected target.",
  "details": {},
  "request_id": "req_..."
}
```

### 7.2 CLI WebSocket

WebSocket 传输：

- Agent 文本增量；
- 当前步骤；
- 工具状态；
- Todo/Hypothesis/Evidence 安全摘要；
- 子任务状态；
- approval pending；
- pause/cancel/complete；
- 心跳、gap 和恢复信号。

统一 envelope：

```json
{
  "schema_version": 1,
  "event_id": "evt_...",
  "sequence": 1842,
  "event_type": "tool.completed",
  "session_id": "ses_...",
  "investigation_id": "inv_...",
  "occurred_at": "2026-08-24T14:32:11Z",
  "payload": {}
}
```

要求：

- sequence 持久化并单调递增；
- 支持按 session/target/investigation/type 过滤；
- 重连携带最后 sequence；
- 服务端循环补齐全部 backlog，不能沿用当前 WS 固定回放 1000 条后直接进入 live 的缺口；
- 未知 event type 可安全忽略；
- 明确 heartbeat、重复投递、slow consumer 和 gap 语义；
- gap 时客户端重新读取权威快照；
- WebSocket 断开不等同于 cancel。

### 7.3 Web HTTP 与 SSE

Web 使用 HTTP 获取资源快照和历史查询；SSE 用于低频单向通知：

- 服务状态变化；
- Investigation 状态；
- 新 Evidence/Conclusion；
- Issue 和解决结果更新。

建议一个 workspace 级复用 SSE，不为每个组件建立连接。SSE 事件主要表达“资源已变化”，客户端通过 TanStack Query invalidate 后重新读取权威资源。

SSE 需要：

- stable event id；
- reconnect cursor/`Last-Event-ID`；
- heartbeat；
- no-cache；
- 反向代理禁用 buffering；
- 认证和连接过期处理；
- durable backfill 与 gap 恢复。

### 7.4 Web 日志 WebSocket

日志高频流使用 WebSocket，因为需要 pause/resume、动态订阅条件、cursor 与流量控制。协议必须修正当前实现：

- 握手接受 `after_cursor`；
- 只补齐缺失记录，不从第一条无限 replay；
- LogRecord 和 heartbeat 使用同一 versioned envelope；
- 服务端分页回放，避免无界 `seen_dedupe_keys`；
- 客户端明确 ack/backpressure 或服务端定义 slow-consumer 断开；
- 重连先补历史，再进入 live；
- 浏览器只收到脱敏 LogRecordView。

## 8. 数据流

### 8.1 调查流

```text
用户在 CLI 输入自然语言
        ↓
POST Agent Session Message
        ↓
服务端创建 durable Operation
        ↓
Agent Runtime 通过 SSH/typed tools 调查
        ↓
Evidence/Log/Events 持久化
        ├── CLI WS：行动摘要、流式回复、审批
        └── Web SSE/WS：日志、状态、Issue、结果
```

### 8.2 审批流

```text
Agent 提出危险 ToolCall
        ↓
服务端持久化 ToolCall + exact Approval intent
        ↓
CLI 收到 approval.pending
        ↓
用户 approve/reject + reason
        ↓
服务端验证 principal、hash、expiry、scope、当前状态
        ↓
消费一次并执行，或拒绝
        ↓
结果写入 Evidence/ChangeSet/Event
        ↓
CLI 更新卡片；Web 只读更新调查状态和结果
```

### 8.3 Web 日志流

```text
Web 选择 target/service/time/filter
        ↓
HTTP cursor 查询历史快照
        ↓
WebSocket(after_cursor) 补齐并进入实时
        ↓
TanStack Virtual 渲染可见日志
        ↓
Evidence/Issue 点击修改 URL 筛选并定位对应上下文
```

## 9. 安全与部署约束

### 9.1 当前状态

当前 FastAPI API 是本地单用户、无入站认证、无 CORS/CSRF/session 的服务；SQLite、broker、SSH session 和活跃任务要求单进程。不得将当前服务直接暴露到公网或共享内网。

当前 SSH adapter 的默认 `known_hosts=()` 行为会禁用主机身份验证，必须在中心服务部署前修复。目标 host-key 策略必须显式、可审计，并在 `/target test` 中显示验证结果。

### 9.2 第一阶段安全基线

- CLI 与 Web 使用统一 authenticated principal；
- 中心服务绑定和网络暴露有明确策略；
- 同源 Web 优先使用 HttpOnly、Secure、SameSite session cookie，并对 mutation 防 CSRF；
- CLI 使用安全存储的短期 token/profile；
- WebSocket/SSE 建连时认证，敏感动作继续逐项授权；
- Trusted Host、TLS reverse proxy、request ID、rate/size limits；
- 默认关闭或保护 `/docs`、`/redoc`、`/openapi.json`；
- actor 来自认证，不信任客户端 `created_by`；
- Target、service、investigation 和 approval 做资源级授权；
- 原始凭据、provider key、SSH transport、未脱敏日志和备份明文永不进入客户端 response。

多用户、OIDC 和 RBAC 可以分阶段落地，但在部署到共享网络前，至少必须有可靠的入站身份、资源授权和审批 actor。

### 9.3 单进程约束

在迁移到共享数据库和 broker 前：

- Uvicorn 必须单 worker；
- 不水平扩容；
- SQLite 放在本地持久卷；
- durable store 是恢复来源，内存 broker 只做 live fan-out；
- 关键 Operation 不只依赖内存 task；
- readiness 与 liveness 分离；
- shutdown 给 Agent、日志订阅和 SSH 足够收尾时间。

## 10. 部署

### 10.1 Web 与后端

第一版采用单部署单元：

```text
Node build stage: vite build
             ↓
将 dist/ 复制进 Python wheel/container
             ↓
FastAPI 同源提供
  /             React SPA shell
  /assets/*     content-hash 静态资源
  /api/*        HTTP API
  /events/*     SSE
  /ws/*         WebSocket
```

运行时不需要 Node.js。前端 API base 使用相对 `/api`，避免 build-time 环境变量锁死镜像。SPA deep link 需要 `index.html` fallback，但不得拦截 `/api`、`/events`、`/ws` 或静态资源 404。

未来前端可独立发布到 Nginx/CDN，但仍通过同域 Gateway 暴露，避免不必要的 CORS、Cookie 和 SSE/WS 凭据复杂度。

### 10.2 CLI

- MVP：npm 包；
- 稳定后：平台签名二进制；
- macOS arm64/x64、Linux x64/arm64、Windows x64 为首要矩阵；
- 每个平台执行真实 binary PTY smoke test；
- 发布 checksum、签名和版本兼容信息；
- CLI 启动时检查 API/stream schema compatibility。

## 11. 错误与恢复

1. **CLI 与中心服务断线：** CLI 保留最后 sequence，重连、补齐、再恢复 live；不取消 Operation。
2. **Web 日志断线：** 使用 log cursor 补齐后恢复 live。
3. **中心服务与远程主机断线：** 只读操作可按明确策略重试；mutation 不盲目重试。
4. **模型失败：** 保留 Session、Operation、已有 Evidence，允许继续输入或显式 `/retry`。
5. **危险操作结果不确定：** 标记 UNCERTAIN，禁止自动重放，要求人工确认远程真实状态。
6. **审批超时或重复：** 服务端返回稳定错误码；single-use 语义不因客户端重试改变。
7. **事件 gap：** 客户端读取权威快照并重设 cursor，不假装历史完整。
8. **慢客户端：** 有界队列；服务端发出 gap/slow-consumer 信号或断开，客户端按快照恢复。
9. **进程重启：** durable Operation、Investigation、approval 和 cursor 驱动 recovery；内存连接可重建。
10. **版本不兼容：** CLI/Web 在握手阶段收到明确的 minimum client/schema version，而不是以解析异常失败。

## 12. 测试策略

### 12.1 后端

- Target facade 与内部 registry 映射；
- Agent Session/Message/Operation 状态机；
- HTTP idempotency；
- 事件过滤、分页回放、重复、gap、heartbeat 和 slow consumer；
- 日志 cursor、历史补齐、live 切换和断线恢复；
- Approval 过期、重复消费、actor、reason 与 downstream failure；
- SSH host-key、断线和 UNCERTAIN no-replay；
- Web 只读 facade 不暴露执行动作；
- OpenAPI response 与错误契约；
- lifespan 恢复和有序关闭。

### 12.2 CLI

- Slash command registry、parser、completion 和 command palette；
- 普通文本与 `/` 指令路由；
- Ink 消息、工具卡片、审批卡片和状态更新；
- 流式 token 合并、重复事件去重和 sequence 恢复；
- HTTP/WS contract mock；
- Session 退出、恢复和 `/cancel` 语义；
- 真实 PTY：中文宽字符、resize、Ctrl+C、Esc、断网、重连；
- npm 安装和最终 binary smoke test。

### 12.3 Web

- URL 中的 target/service/time/filter 状态；
- TanStack Query 快照与 event invalidation；
- 日志 cursor 分页和虚拟滚动；
- 自动滚动、暂停、恢复；
- stack/JSON 展开和关键词高亮；
- Evidence/Issue 到日志定位；
- stream 断线补齐和 gap 快照恢复；
- 页面中不存在审批和执行入口；
- Vitest + React Testing Library；
- Playwright 覆盖真实导航和实时更新。

### 12.4 真实验收

```text
1. 只配置主机、用户名和安全 SSH 认证
2. 不提供本地项目源码
3. 运行 incidentlens，并通过 /target 选择目标
4. 输入自然语言调查请求
5. Agent 自动发现远程服务并采集日志
6. Web 独立打开后清晰展示同一远程日志和服务状态
7. CLI 展示行动摘要并处理危险修改审批
8. Agent 修复、验证，必要时完成回滚演练
9. Web 展示根因、证据、修复和修复前后结果
10. CLI 或 Web 断线后恢复，权威状态和日志无静默缺口
```

## 13. 分阶段实施

### 阶段 1：后端产品接口基础

1. 修复 SSH host-key 默认行为；
2. 明确入站认证和 principal；
3. API v1、显式 response model 与统一 error；
4. Target facade；
5. Agent Session/Message；
6. durable Operation；
7. versioned/filterable/replayable event stream；
8. cursor-based log history/live protocol；
9. Approval detail 与 actor/reason；
10. Web overview/service/issue/investigation summary facade。

### 阶段 2：React Ink CLI MVP

完成：

```text
incidentlens
→ /target add
→ /target test
→ /target production
→ 自然语言调查
→ 实时行动摘要
→ 继续补充消息
→ approve/reject
→ 退出并恢复 Session
```

第一版不做插件市场、复杂主题系统、CLI 日志大屏或本地执行器。

### 阶段 3：Web 日志工作台 MVP

按顺序实现：

1. 总览和服务状态；
2. 服务详情；
3. 历史日志搜索；
4. 实时日志、虚拟滚动和断线补齐；
5. 当前 Issue；
6. 根因、Evidence、Resolution、Verification；
7. 只读 Investigation 里程碑。

优先把日志筛选、结构、上下文和流恢复做正确，再增加复杂图表。

### 阶段 4：部署与生产化

- 多阶段单镜像构建；
- npm CLI 发布与 binary 评估；
- 单 worker 和持久卷声明；
- TLS、认证、secret、readiness、backup 和 migration；
- 契约兼容 CI；
- 出现真实水平扩展需求后，再迁移共享数据库、broker 和任务执行基础设施。

## 14. 非目标

第一轮不包含：

- Web 内 Agent 对话或审批；
- CLI 内完整日志浏览器；
- CLI 到 Web 的链接或自动打开浏览器；
- 强制本地源码；
- 用户先在 Web 建项目；
- CLI 本地 SSH/模型执行器；
- Next.js SSR/BFF；
- Rust CLI；
- 插件市场；
- 无审批自动修复；
- 多区域、多控制面或立即水平扩展；
- 在 domain 核心之外再造第二套 Agent Runtime。

## 15. 外部技术依据

- Ink: <https://github.com/vadimdemedes/ink>
- Ink Testing Library: <https://github.com/vadimdemedes/ink-testing-library>
- Bun standalone executables: <https://bun.sh/docs/bundler/executables>
- Node single executable applications: <https://nodejs.org/api/single-executable-applications.html>
- Vite static deployment: <https://vite.dev/guide/static-deploy.html>
- Vite backend integration: <https://vite.dev/guide/backend-integration.html>
- TanStack Query: <https://tanstack.com/query/latest/docs/framework/react/overview>
- TanStack Router: <https://tanstack.com/router/latest/docs/framework/react/overview>
- TanStack Table: <https://tanstack.com/table/latest/docs/introduction>
- TanStack Virtual: <https://tanstack.com/virtual/latest/docs/introduction>
- Apache ECharts: <https://echarts.apache.org/handbook/en/basics/import/>
- FastAPI WebSocket: <https://fastapi.tiangolo.com/advanced/websockets/>
- FastAPI static files: <https://fastapi.tiangolo.com/tutorial/static-files/>
- MDN SSE: <https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events>
- OWASP WebSocket Security: <https://cheatsheetseries.owasp.org/cheatsheets/WebSocket_Security_Cheat_Sheet.html>

## 16. 完成判据

本设计完成后的产品必须满足：

1. 用户运行 `incidentlens` 后，以自然语言驱动云端 Agent；
2. `/` 指令可发现地管理目标、会话、状态和审批；
3. 没有本地源码时仍能完成真实 SSH 调查；
4. CLI 不承担完整日志展示，也不依赖 Web 链接；
5. Web 独立、清晰、实时地展示日志、服务状态、问题和解决结果；
6. Web 不执行 Agent 工具或审批；
7. FastAPI 是唯一远程执行、安全策略和审计边界；
8. 客户端断线后通过 durable cursor 恢复，不产生静默缺口；
9. 危险操作保持精确审批、备份、验证、回滚和 UNCERTAIN no-replay；
10. Web 和 CLI 共享协议与权威状态，但不重复彼此的产品职责。
