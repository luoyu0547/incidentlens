# IncidentLens Phase 3：混合日志采集与证据存储设计

**日期：** 2026-08-12  
**状态：** 已通过设计方向确认，等待书面规格复核  
**来源：** `docs/superpowers/plans/2026-08-10-incidentlens-delivery-roadmap.md`

## 1. 目标与范围

Phase 3 在现有 FastAPI、SQLite、AsyncSSH 模块化单体中加入完整的混合日志调查能力，不增加远端 agent、独立 worker 或消息队列。

本阶段交付：

1. 收口 Phase 2 遗留的容器目录列举与内容搜索。
2. 对注册的宿主机日志文件、容器内日志文件和 Docker stdout/stderr 执行按需查询。
3. 建立显式 opt-in、持久化且可在控制面重启后恢复的流式订阅。
4. 使用源游标、幂等键和有界队列实现恢复、去重与背压控制。
5. 只将脱敏日志写入 SQLite，并用 FTS5 建立全文索引。
6. 解析时间戳和严重级，生成确定性的正常日志信号与服务关联键。
7. 建立只追加、不可修改的 Evidence Store；证据内容始终为脱敏文本。
8. 提供日志查询、搜索、订阅管理、增量读取及证据查询 API。
9. 提供单元、Web API、运行时生命周期和 opt-in live SSH/Docker 验收测试。

## 2. 非目标

Phase 3 不实现：

- 远端常驻 agent、独立采集进程或消息队列。
- 多节点控制面协调、分布式租约或横向扩展。
- 模型供应商接入、AgentRuntime、Web 前端或自动修复。
- 任意 shell、任意 Docker 参数或客户端提供 SSH 连接信息。
- 原始日志持久化、长期归档、压缩或完整 retention policy。
- 机器学习异常检测；正常日志信号仅为可解释的确定性规则。
- 完整 incident 生命周期；本阶段只把 `incident_id` 当作外部引用。

## 3. 关键决策

### 3.1 模块化单体内扩展

沿用现有 runtime service container，由 `runtime.py` 统一创建日志、订阅和证据服务；FastAPI lifespan 负责启动持久订阅并在关闭 SSH session 前停止采集任务。

新增包：

```text
incidentlens_control_plane/
├── logs/
│   ├── types.py
│   ├── parser.py
│   ├── redaction.py
│   ├── signals.py
│   ├── correlation.py
│   ├── sources.py
│   ├── store.py
│   ├── service.py
│   └── subscriptions.py
├── evidence/
│   ├── types.py
│   └── store.py
└── routes/
    ├── logs.py
    └── evidence.py
```

各单元职责：

- `sources`：从受信注册信息构造固定远程操作，输出原始日志行。
- `parser`：解析时间戳、严重级和结构化字段。
- `redaction`：在任何持久化、事件发布或 API 响应前移除敏感信息。
- `signals`：产生确定性的正常日志标签。
- `correlation`：提取 request、trace、span 等关联键。
- `store`：持久化脱敏记录、订阅、游标与 FTS 索引。
- `subscriptions`：管理进程内采集任务、恢复、重试、去重与背压。
- `evidence`：只追加不可变的脱敏证据引用。
- `service`：协调注册表、路径策略、日志源、处理管线、存储和证据。

### 3.2 持久订阅语义

持续采集必须由用户显式创建并设置 `opt_in_streaming=true`。订阅状态持久化为：

- `active`：运行中，应用重启后自动恢复。
- `paused`：停止采集并保留游标，不自动恢复。
- `error`：连续失败达到阈值，等待显式 resume。
- `deleted`：逻辑删除，不再恢复。

仅 `active` 且 opt-in 的订阅在启动时恢复。

### 3.3 只保存脱敏内容

原始日志只允许短暂存在于函数局部变量或有界内存队列中。以下位置禁止出现原文：

- SQLite 日志记录和 FTS 索引。
- Evidence Store。
- runtime event payload。
- HTTP/WebSocket 响应。
- 错误详情、异常文本和应用日志。

Evidence Store 的哈希基于脱敏内容计算，而不是基于原文计算。

## 4. Phase 2 收口：容器 List/Search

现有 `ContainerFileBackend` 已支持容器内读取和 stat，但 gateway 对容器 `list_dir` 和 `search` 仍返回 unsupported。Phase 3 的第一项任务补齐该能力。

要求：

- `ContainerFileBackend.list_directory()` 返回有界 `FileMetadata`。
- `ContainerFileBackend.search()` 沿用宿主机搜索限制：最多扫描 10,000 个文件、每个文件最多 1 MiB、最多返回 200 个匹配。
- 容器必须来自 service registration 的 `container_names`。
- 路径必须位于 `allowed_container_paths` 内。
- 不跟随符号链接读取目标内容。
- 只使用固定 argv 模板，不生成 shell 字符串，不允许用户控制命令参数。

若目标容器缺少实现所需的固定工具，返回明确的 capability error，不降级为上传脚本或临时解释器。

## 5. 领域模型

### 5.1 日志来源

`LogSourceKind`：

- `file`：宿主机或注册容器内的白名单日志文件。
- `docker`：注册容器的 Docker stdout/stderr。

每个请求和订阅都绑定：

- `project_id`
- `target_id`
- `service`
- `source_kind`
- `scope`：host 或 container
- `source_ref`：安全路径或注册容器名

客户端不能提交 host、port、SSH user、credential 或任意 Docker flags。

日志文件首先受 `ServiceRegistration.allowed_log_paths` 约束；若该字段为空，退回相应的 `allowed_host_paths` 或 `allowed_container_paths`。所有路径仍需通过 `RemotePathPolicy` 的绝对路径、`..`、root 和符号链接检查。

### 5.2 日志记录

持久化记录包含：

- `log_id`
- 可选 `subscription_id`
- project、target、service
- source kind 和安全 source ref
- source cursor
- `dedupe_key`
- `observed_at`
- 可选 `event_time`
- severity
- `message_redacted`
- redaction summary
- 可选 normal signal
- 可选 correlation key
- 可选 evidence reference id

不存在 raw message 字段。若无法解析日志时间，`event_time` 为空，使用 `observed_at` 排序。单条脱敏文本上限为 16 KiB，超出后截断并记录截断标记。

### 5.3 严重级

支持：`trace`、`debug`、`info`、`notice`、`warn`、`error`、`critical`、`unknown`。

解析顺序：

1. JSON 字段：`severity`、`level`、`log.level`、`lvl`。
2. 常见 syslog/文本 token，大小写不敏感。
3. 无匹配时设为 `unknown`。

JSON 解析失败必须退回文本解析，不中断采集。

### 5.4 正常日志信号

确定性标签包括：

- `heartbeat`
- `healthcheck_ok`
- `request_ok`
- `startup`
- `shutdown`
- `retry`

无法识别时为空。该标签只用于可解释筛选，不代表统计基线或异常判断。

### 5.5 服务关联

优先从结构化字段或常见文本格式提取：

1. trace id
2. request id
3. span id
4. 其他明确 correlation id

若无标识则不生成 correlation key；不使用仅由 service/container 构成的伪关联键，以免把不相关日志错误归组。

## 6. 日志源

统一协议：

```python
class LogSource(Protocol):
    async def query(self, request: LogQueryRequest) -> tuple[RawLogLine, ...]: ...
    async def stream(self, subscription: LogSubscription) -> AsyncIterator[RawLogLine]: ...
```

### 6.1 文件日志

按需查询默认从文件尾部读取有限字节或有限行，不全量加载大文件。流式采集使用 SFTP stat/read 的轮询方式，默认每 2 秒检查一次，从已持久化 offset 增量读取，不执行 `tail -f` shell 命令。

文件游标记录：

- 安全 source identity
- 文件 generation：初版使用修改时间和 size
- byte offset

若 size 小于 offset，视为 truncate/rotate，发布 `log.source_rotated` 事件，并按订阅配置从文件起点或有限 tail 重新开始。初版统一从起点开始，以保证旋转后的新日志不被遗漏。

### 6.2 Docker 日志

只允许针对注册容器执行服务端构造的固定 argv：

- 按需：`docker logs --timestamps --tail <bounded> -- <container>`
- 流式：`docker logs --timestamps --follow --since <cursor-time> -- <container>`

`tail` 有服务端上下限；客户端不能注入额外 flags。Docker CLI 自身的错误输出不作为应用日志保存，只产生脱敏错误摘要。

Docker 游标记录最后成功提交的时间戳及批内序号。恢复时使用 `--since` 重放边界数据，再由幂等键消除重复。

## 7. 处理管线

所有按需和流式数据共享同一管线：

1. 解析 timestamp、severity 和结构化字段。
2. 立即脱敏消息与允许保留的属性。
3. 丢弃原始行引用。
4. 计算 normal signal。
5. 提取 correlation key。
6. 计算基于来源、游标和脱敏消息的 `dedupe_key`。
7. 根据请求选择仅返回、持久化和/或创建证据。
8. 持久化后推进游标。

游标只能在对应批次的日志记录和 FTS 索引事务成功提交后推进。进程在提交前退出会导致边界数据重读，由唯一 `dedupe_key` 保证幂等；不得先推进游标再写记录。

## 8. SQLite 与 FTS5

### 8.1 `log_records`

关键字段：

```sql
log_id TEXT PRIMARY KEY,
subscription_id TEXT,
project_id TEXT NOT NULL,
target_id TEXT NOT NULL,
service_name TEXT NOT NULL,
source_kind TEXT NOT NULL,
source_ref TEXT NOT NULL,
cursor TEXT NOT NULL,
dedupe_key TEXT NOT NULL UNIQUE,
observed_at TEXT NOT NULL,
event_time TEXT,
severity TEXT NOT NULL,
message_redacted TEXT NOT NULL,
redaction_summary_json TEXT NOT NULL,
normal_signal TEXT,
correlation_key TEXT,
created_at TEXT NOT NULL
```

索引覆盖 scope/time、subscription、severity 和 correlation key。

### 8.2 FTS5

FTS 只索引 `message_redacted`。`append_batch()` 在同一 SQLite transaction 内写 `log_records` 和 FTS 数据；任何一步失败则全部回滚。用户搜索只支持长度受限的简单 token/phrase 查询，由服务端转义，不直接接受任意 FTS5 表达式。

### 8.3 `log_subscriptions`

保存订阅定义、opt-in 标志、状态、创建/更新时间、最近启动/停止时间、错误计数和脱敏后的最后错误摘要。

### 8.4 `log_cursors`

每个订阅一条游标，保存 source-specific cursor、source generation、最近 observed time 和更新时间。

### 8.5 `log_subscription_runs`

保存每次运行的开始、结束、状态和脱敏错误，用于审计恢复行为，不保存日志内容。

## 9. 持久订阅、去重与背压

每个 active subscription 在当前进程内拥有：

- 一个 source reader task
- 一个 `asyncio.Queue(maxsize=1000)`
- 一个 batch writer task

writer 每批最多提交 100 条记录。默认同时 active 的订阅上限为 20，作为 runtime setting 暴露；超出时 API 返回 429。

### 9.1 去重

`dedupe_key` 由 project、target、service、source identity、cursor 和脱敏消息计算。数据库 UNIQUE 约束是最终幂等保证，重复写使用 `INSERT OR IGNORE` 或等价事务逻辑。

### 9.2 背压

- 文件轮询在队列满时暂停读取，自然施加背压。
- Docker follow 若队列在配置超时内仍满，则关闭远端流，保存最后已提交游标，发布 `log.backpressure`，随后按恢复逻辑重连。
- 不允许无限增长内存，也不静默丢弃已读取但未提交的数据。

### 9.3 错误恢复

失败采用有上限的指数退避，最大 60 秒。连续失败达到阈值后订阅进入 `error`，停止自动重试并等待用户 resume。错误摘要必须先脱敏；不得将原始 stdout、stderr、host 或 credential 写入数据库和事件。

启动、暂停、恢复、rotate、背压、错误和批次写入分别发布安全 runtime events。WebSocket 断开不得停止采集任务。

## 10. Evidence Store

Evidence Store 是只追加的脱敏证据库。

`evidence_refs` 包含：

- `evidence_ref_id`
- 外部 `incident_id`
- evidence kind
- project、target、service
- source kind、source ref、cursor
- `content_redacted`
- `content_sha256`
- redaction summary
- 安全 metadata：severity、event time、normal signal、correlation key
- `created_at`、`created_by`

约束：

- 不提供 update API。
- `content_sha256` 对脱敏文本计算。
- 相同 source/cursor/hash 的创建必须幂等。
- Evidence Store 只接受已经经过日志处理管线的值，不接受任意 raw text 参数。
- Phase 3 不实现删除或 tombstone；后续若有治理需求，只能新增审计型状态，不覆盖原记录。

## 11. API

### 11.1 按需查询与索引搜索

- `POST /api/logs/query`
  - 查询远端 file 或 Docker source。
  - 默认只返回脱敏结果。
  - `persist=false` 默认不入库。
  - `persist=true` 写入日志库与 FTS。
  - `create_evidence=true` 时要求 `incident_id`，并创建脱敏证据。
- `GET /api/logs/search`
  - 在已持久化的脱敏日志上按 scope、时间、severity、correlation 和简单全文查询过滤。

### 11.2 持久订阅

- `POST /api/logs/subscriptions`
- `GET /api/logs/subscriptions`
- `GET /api/logs/subscriptions/{subscription_id}`
- `POST /api/logs/subscriptions/{subscription_id}/pause`
- `POST /api/logs/subscriptions/{subscription_id}/resume`
- `DELETE /api/logs/subscriptions/{subscription_id}`
- `GET /api/logs/subscriptions/{subscription_id}/records`
- `WS /api/logs/subscriptions/{subscription_id}/ws`

创建订阅必须显式提交 `opt_in_streaming=true`。订阅 WebSocket 采用现有 events 路由的策略：先注册 live queue，再 replay durable records，最后进入 live stream；通过 cursor 和 dedupe key 消除 replay/live 重叠。

### 11.3 证据

- `POST /api/evidence/from-log-records`
- `GET /api/evidence/{evidence_ref_id}`
- `GET /api/incidents/{incident_id}/evidence`

所有请求模型使用 `extra="forbid"`。错误映射：

- 400：字段组合语义冲突。
- 404：注册对象、订阅或证据不存在。
- 409：容器不属于 service 或状态转换冲突。
- 422：输入、查询语法或 source capability 不受支持。
- 429：active subscription 超过上限。
- 502：脱敏后的远程 source unavailable。
- 504：远程操作超时。

## 12. Runtime 生命周期

`build_runtime()` 创建 LogStore、EvidenceStore、LogService 和 LogSubscriptionManager，并执行幂等 migration。

FastAPI lifespan 顺序：

1. 构建 runtime。
2. 恢复所有 active opt-in subscriptions。
3. 接收请求。
4. shutdown 时先 `subscriptions.close_all()`，等待 reader/writer 保存最后已提交状态。
5. 再 `sessions.close_all()`。

若个别订阅恢复失败，应用仍可启动；失败订阅记录脱敏错误并按退避重试或进入 `error`。数据库 migration 失败则应用启动失败，因为无法保证日志与游标一致性。

## 13. 测试策略

### 13.1 单元测试

新增：

- `tests/logs/test_redaction.py`
- `tests/logs/test_parser.py`
- `tests/logs/test_signals.py`
- `tests/logs/test_correlation.py`
- `tests/logs/test_store.py`
- `tests/logs/test_sources_file.py`
- `tests/logs/test_sources_docker.py`
- `tests/logs/test_subscriptions.py`
- `tests/evidence/test_store.py`

覆盖：脱敏规则、JSON/文本严重级、时间戳、正常信号、关联键、migration、FTS、事务一致性、幂等去重、游标恢复、rotate、重试与背压。

### 13.2 Remote ops 与 Web API 测试

扩展 fake transport 记录固定 Docker argv 和流式输出。新增：

- 容器 list/search 安全边界与有界结果。
- Docker logs 只接受注册容器。
- 不生成 shell string。
- query 的 persist/evidence 组合。
- opt-in 订阅状态机。
- HTTP 错误不泄露原文。
- WebSocket replay/live 去重。

### 13.3 Opt-in live 验收

新增 `tests/integration/test_live_log_tools.py`，默认跳过，仅在显式环境变量和 Docker/SSH 可用时运行。验证：

1. 容器 list/search。
2. 宿主机日志文件按需查询与脱敏。
3. Docker logs 按需查询。
4. 流式订阅接收新增日志。
5. 应用重启后从游标恢复。
6. 边界重放不产生重复记录。
7. Evidence Store 只含脱敏内容。
8. teardown 清理测试容器和任务。

## 14. 任务拆分与验收标准

### Task 1：补齐容器 List/Search

- 容器 list/search 支持注册容器和白名单路径。
- 保持既有 host 行为。
- 结果有界且不跟随 symlink。
- 所有远程执行使用固定 argv。

### Task 2：日志模型、解析、脱敏与关联

- JSON、文本和 syslog-like severity fixtures 通过。
- token、password、private key、URL secret、email/IP 等规则通过。
- redaction summary 正确。
- 正常信号和 correlation key 为确定性结果。
- 过长消息安全截断。

### Task 3：LogStore、订阅表、游标与 FTS5

- migration 幂等。
- records 与 FTS 同事务提交。
- dedupe key 唯一约束有效。
- scope、时间、severity、correlation 和全文过滤有效。
- schema 中不存在 raw message。

### Task 4：按需 File/Docker source 与查询 API

- file path 和 container 均从注册边界解析。
- Docker argv 固定、有界。
- 所有响应已脱敏。
- `persist=false` 不落库；`persist=true` 可被 FTS 搜索。
- capability 和远程错误安全映射。

### Task 5：Evidence Store 与 API

- 只能从脱敏 LogRecord 创建证据。
- 内容不可更新，重复创建幂等。
- 哈希基于脱敏内容。
- incident evidence 可有界列举。
- API 永不返回 raw content。

### Task 6：持久订阅与生命周期恢复

- active opt-in subscription 自动启动。
- pause 保存游标并停止任务。
- resume 从最后提交游标继续。
- 应用重启恢复 active subscriptions。
- file rotate/truncate 和 Docker reconnect 有测试。
- shutdown 先停止订阅，再关闭 SSH session。

### Task 7：背压、重试与安全事件

- 队列和 batch 大小有界。
- Docker 背压时安全断开并从已提交游标恢复。
- 错误达到阈值后进入 `error`。
- runtime events 和错误记录不含原始日志或凭据。
- WebSocket 断开不影响采集。

### Task 8：完整查询、订阅和 WebSocket API

- 所有端点具备严格请求模型、状态冲突和 limit 校验。
- WebSocket 先 replay 后 live，且不重复。
- FTS 输入不允许任意表达式注入。
- API 集成测试覆盖 happy path、拒绝路径和远程失败。

### Task 9：Live verification 与文档收口

- 默认测试保持离线。
- opt-in 验证 file、Docker、streaming、restart、dedupe 和 evidence。
- 更新 README 与 Phase 3 verification 文档，使实现和声明一致。
- 全量 unit tests、Ruff 和 live acceptance 通过。

## 15. Phase 3 完成门槛

只有满足以下条件，Phase 3 才可标记完成：

1. Phase 2 的容器 List/Search 缺口已实现并通过安全测试。
2. 注册文件和 Docker 日志均支持有界按需查询。
3. opt-in active subscriptions 可跨应用重启恢复。
4. 游标推进与日志/FTS 提交具有一致性，边界重放不会产生重复记录。
5. 内存队列、批次、单条消息、并发订阅和远程查询均有硬上限。
6. severity、normal signals 与 service correlation 规则均有确定性测试。
7. SQLite FTS5 只索引脱敏日志。
8. Evidence Store 只保存脱敏内容且不可修改。
9. API、runtime events、错误和应用日志均不泄露 credential 或原始敏感日志。
10. 应用 shutdown 不遗留采集任务或在 session 关闭后继续使用 transport。
11. 默认离线测试、Ruff 和 opt-in live SSH/Docker 验收全部通过。
12. README、路线图和 verification 文档与实际能力一致。

## 16. 现有代码落点

实施将主要沿用并修改：

- `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- `apps/control-plane/src/incidentlens_control_plane/main.py`
- `apps/control-plane/src/incidentlens_control_plane/project_registry/types.py`
- `apps/control-plane/src/incidentlens_control_plane/remote_ops/policy.py`
- `apps/control-plane/src/incidentlens_control_plane/remote_ops/files.py`
- `apps/control-plane/src/incidentlens_control_plane/remote_ops/gateway.py`
- `apps/control-plane/src/incidentlens_control_plane/remote_ops/transport.py`
- `apps/control-plane/src/incidentlens_control_plane/remote_ops/asyncssh_adapter.py`
- `apps/control-plane/src/incidentlens_control_plane/events/store.py`
- `apps/control-plane/src/incidentlens_control_plane/events/broker.py`
- `apps/control-plane/src/incidentlens_control_plane/routes/events.py`
- `tests/remote_ops/`
- `tests/web/`
- `tests/integration/`
