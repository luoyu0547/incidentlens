# Phase 5: CLI、Web UI、报告与端到端验收设计

日期：2026-08-13
状态：设计评审通过，等待规格复核

## 1. 目标

Phase 5 为 IncidentLens 添加用户交互层：Claude Code 风格的富终端 CLI、Jinja2 + HTMX 本地 Web UI、调查报告生成（Markdown + HTML 双格式），以及完整的 Docker Compose 微服务验收环境。

Phase 1–4 的后端能力（项目注册、SSH 远程操作、日志采集、证据存储、有界 Agent 运行时）保持不变。Phase 5 不修改任何安全边界、策略引擎或备份门禁。

## 2. 设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| Web UI 交付方式 | Jinja2 + HTMX，FastAPI 直接托管 | 本地单用户工具，零外部依赖，单进程启动 |
| CLI 框架 | Rich / Textual | 富终端交互，实时进度展示，流式事件订阅 |
| CLI 与后端通信 | 直接调用 Python 模块（不走 HTTP） | 本地工具零网络开销，Textual 流式体验更自然 |
| 报告格式 | Markdown + HTML 双格式 | Markdown 可移植，HTML 浏览器直接打开 |
| 验收环境 | 完整微服务模拟 Docker Compose | 模拟真实多服务故障场景 |
| 前端样式 | Pico CSS（classless） | ~10KB，无需构建工具 |

## 3. 架构概览

```
┌─────────────┐     ┌─────────────┐
│  CLI (TUI)  │     │  Web UI     │
│  Textual    │     │  Jinja2+HTMX│
└──────┬──────┘     └──────┬──────┘
       │ 直接调用          │ HTTP/WS
       │ Python 模块       │
       ▼                   ▼
┌──────────────────────────────────┐
│         FastAPI Runtime          │
│  ┌────────────────────────────┐  │
│  │     InvestigationService   │  │
│  │     ReportService (新增)    │  │
│  │     LogService             │  │
│  │     ApprovalService        │  │
│  │     EvidenceService        │  │
│  └────────────────────────────┘  │
│  ┌────────────────────────────┐  │
│  │     AgentOrchestrator      │  │
│  │     ToolExecutor           │  │
│  │     RecoveryService        │  │
│  └────────────────────────────┘  │
└──────────────────────────────────┘
```

CLI 直接导入 Python 模块调用 service 层；Web UI 通过 HTTP API 访问同一 service 层。核心业务逻辑完全共享，仅传输层不同。

## 4. 新增模块

### 4.1 报告服务（reports/）

```
incidentlens_control_plane/reports/
├── __init__.py
├── types.py          # ReportBundle, ReportSection, ReportMetadata
├── markdown.py       # Markdown 渲染器
├── html.py           # HTML 渲染器（内嵌 CSS，diff 高亮，证据折叠）
└── service.py        # ReportService
```

#### ReportService

```python
class ReportService:
    def __init__(self, *, investigations: InvestigationStore,
                 evidence: EvidenceStore, changes: ChangeSetStore,
                 output_dir: Path):
        ...

    def generate(self, investigation_id: str) -> ReportBundle:
        """从调查数据生成 Markdown + HTML 报告。"""
        ...
```

#### ReportBundle

```python
class ReportBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: str
    markdown_path: Path
    html_path: Path
    metadata: ReportMetadata

class ReportMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symptom: str
    root_cause: str | None
    confidence: float | None
    services_affected: list[str]
    evidence_count: int
    tool_calls_count: int
    duration_seconds: float
    generated_at: datetime
```

#### 报告内容结构

按设计规格第 11 节组织：

1. **摘要** — 症状、影响范围、持续时间
2. **根因分析** — 已确认根因或带置信度的推测
3. **时间线** — 关键事件按时间排序（调查启动、工具调用、假设变化、结论）
4. **证据汇总** — 日志、配置、文件、命令证据引用，按类型分组
5. **假设演进** — 已确认 / 已排除 / 仍活跃的假设
6. **修改记录** — 远程文件修改 diff（如果有）
7. **验证结果** — 修改后的验证输出
8. **备份状态** — 本地和远端备份路径
9. **修复建议** — 正式修复建议和待开发人员完成的工作
10. **附录** — 完整工具调用列表、子 Agent 报告

#### Markdown 渲染器

```python
class MarkdownRenderer:
    def render(self, sections: list[ReportSection]) -> str:
        """生成 GitHub-Flavored Markdown。"""
        ...
```

每个 `ReportSection` 包含标题、内容（Markdown 文本）和可选的元数据。Diff 内容使用 fenced code block 标记语言（`diff`）。

#### HTML 渲染器

```python
class HtmlRenderer:
    def render(self, sections: list[ReportSection], metadata: ReportMetadata) -> str:
        """生成自包含 HTML，内嵌 CSS。"""
        ...
```

HTML 报告特性：
- 内嵌 CSS（Pico CSS 变量 + 自定义样式），浏览器直接打开
- Diff 高亮（绿色添加、红色删除）
- 证据引用可折叠展开
- 时间线可视化（CSS-only 垂直时间线）
- 打印友好（@media print 样式）

### 4.2 CLI 应用（cli/）

```
incidentlens_control_plane/cli/
├── __init__.py
├── app.py                # Textual App 主入口
├── screens/
│   ├── __init__.py
│   ├── dashboard.py      # 仪表盘：活跃调查、待审批、最近活动
│   ├── investigation.py  # 调查详情：时间线、run 列表、工具调用
│   ├── logs.py           # 日志浏览器：按服务/级别搜索
│   ├── approvals.py      # 审批面板：批准/拒绝
│   ├── evidence.py       # 证据查看器
│   └── report.py         # 报告查看器（终端渲染 Markdown）
└── widgets/
    ├── __init__.py
    ├── timeline.py        # 调查时间线组件
    └── tool_call_flow.py  # 工具调用流组件
```

#### CLI 启动方式

```bash
# 默认启动（仪表盘）
incidentlens

# 指定数据目录
INCIDENTLENS_DATA_DIR=~/.incidentlens incidentlens

# 直接查看某个调查
incidentlens investigate <investigation_id>

# 生成报告
incidentlens report <investigation_id>
```

CLI 通过 `build_runtime()` 直接构建本地服务实例，不经过 HTTP。这意味着 CLI 和 Web UI 进程不能同时写入同一个 SQLite 数据库（WAL 模式下可以并发读，但写入需要串行）。MVP 中 CLI 和 Web UI 建议二选一使用，后续可通过 WAL + 文件锁支持并发。

#### 屏幕设计

**仪表盘**
- 顶部：活跃调查数量、待审批数量、今日完成调查数
- 主体：调查列表表格（ID、症状、状态、持续时间、更新时间）
- 底部：快捷键提示（s=新建调查、a=审批、q=退出）

**调查详情**
- 左侧面板：调查元信息（状态、预算使用、证据数）
- 右侧面板：垂直时间线，每个节点显示：
  - 调查状态变化
  - Agent run 启动/完成
  - 工具调用（名称、状态、耗时）
  - 假设提出/确认/排除
  - 子 Agent 委托/报告
  - 证据收集
- 底部：操作栏（启动/取消/恢复/查看报告）

**日志浏览器**
- 顶部过滤栏：服务选择、级别过滤（ERROR/WARNING/INFO）、时间范围
- 主体：日志列表（时间、级别、服务、消息摘要）
- 选中行展开显示完整日志（已脱敏）

**审批面板**
- 待审批项列表（工具名、参数摘要、调查 ID、请求时间）
- 选中后显示完整参数
- `a` 批准、`r` 拒绝、`Enter` 查看详情

#### 实时更新

CLI 通过 `RuntimeEventBroker.subscribe()` 订阅本地事件流：
- 调查状态变化 → 仪表盘自动刷新
- 工具调用完成 → 时间线追加新节点
- 审批请求 → 弹出审批通知
- 子 Agent 报告 → 时间线追加折叠节点

Textual 的 `set_interval` 或 `watch` 机制驱动 UI 刷新，不使用 asyncio 事件循环的原始回调。

### 4.3 Web UI（web/）

```
incidentlens_control_plane/web/
├── __init__.py
├── routes.py             # Web 页面路由（挂在 FastAPI app 上）
├── dependencies.py       # Jinja2 环境配置、模板注入
├── templates/
│   ├── base.html         # 公共布局（导航、侧边栏、flash 消息）
│   ├── dashboard.html
│   ├── investigations/
│   │   ├── list.html
│   │   ├── detail.html
│   │   └── _timeline.html    # HTMX 局部刷新
│   ├── logs/
│   │   ├── search.html
│   │   └── _results.html
│   ├── approvals/
│   │   ├── list.html
│   │   └── _action.html
│   ├── evidence/
│   │   └── detail.html
│   ├── reports/
│   │   └── render.html
│   └── projects/
│       └── manage.html
└── static/
    ├── css/
    │   └── custom.css     # Pico CSS 变量覆盖 + 自定义样式
    └── js/
        └── events.js      # SSE 连接、HTMX 扩展
```

#### 页面路由

| 路由 | 方法 | 功能 |
|---|---|---|
| `GET /` | GET | 仪表盘 |
| `GET /web/investigations` | GET | 调查列表（支持 status/project_id 过滤） |
| `GET /web/investigations/{id}` | GET | 调查详情 + 时间线 |
| `GET /web/investigations/{id}/start` | POST | 启动调查（HTMX 触发） |
| `GET /web/investigations/{id}/cancel` | POST | 取消调查 |
| `GET /web/logs/search` | GET | 日志搜索页面 |
| `GET /web/logs/search` (HTMX) | GET | 日志搜索结果（局部刷新） |
| `GET /web/approvals` | GET | 审批列表 |
| `POST /web/approvals/{id}/approve` | POST | 批准 |
| `POST /web/approvals/{id}/reject` | POST | 拒绝 |
| `GET /web/evidence/{id}` | GET | 证据详情 |
| `GET /web/reports/{id}` | GET | 渲染 HTML 报告 |
| `GET /web/projects` | GET | 项目管理 |
| `POST /web/projects` | POST | 创建项目 |
| `PUT /web/projects/{id}` | PUT | 更新项目 |
| `DELETE /web/projects/{id}` | DELETE | 删除项目 |
| `GET /web/events/stream` | GET | SSE 事件流 |

#### 实时机制

**SSE（Server-Sent Events）** 用于调查进度推送：

```python
@router.get("/web/events/stream")
async def event_stream(request: Request):
    async def generate():
        async for event in broker.subscribe():
            yield f"data: {event.model_dump_json()}\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream")
```

前端通过 `EventSource` 接收，HTMX 的 `hx-trigger="sse:investigation-updated"` 处理局部刷新。

低频数据（仪表盘统计）使用 HTMX 的 `hx-trigger="every 10s"` 轮询。

#### 模板设计

`base.html` 提供：
- 顶部导航栏：仪表盘、调查、日志、审批、项目
- 侧边栏：快速操作（新建调查、查看报告）
- Flash 消息区域
- Pico CSS + 自定义变量

所有模板使用 Jinja2 继承（`{% extends "base.html" %}`），HTMX 属性处理局部刷新（`hx-get`, `hx-post`, `hx-swap="innerHTML"`）。

#### 样式

使用 Pico CSS（classless 模式）作为基础，通过 CSS 变量覆盖：
- 主色调：中性蓝灰（适合运维工具）
- 状态颜色：绿色（成功）、黄色（警告）、红色（错误/失败）、蓝色（运行中）
- 表格：斑马纹、hover 高亮
- 时间线：CSS-only 垂直线 + 节点

### 4.4 Docker Compose 验收环境

```
infra/acceptance/
├── docker-compose.yml
├── services/
│   ├── api-gateway/
│   │   ├── Dockerfile
│   │   └── app.py          # 模拟 API 网关（Flask）
│   ├── order-service/
│   │   ├── Dockerfile
│   │   └── app.py          # 模拟订单服务
│   ├── payment-service/
│   │   ├── Dockerfile
│   │   └── app.py          # 模拟支付服务
│   ├── inventory-service/
│   │   ├── Dockerfile
│   │   └── app.py          # 模拟库存服务
│   └── postgres/
│       ├── Dockerfile
│       └── init.sql        # 数据库初始化
├── scenarios/
│   ├── database-pool-exhaustion.yaml
│   ├── downstream-timeout.yaml
│   ├── deployment-regression.yaml
│   └── dependency-unavailable.yaml
└── README.md
```

#### docker-compose.yml 结构

```yaml
services:
  postgres:
    build: ./services/postgres
    ports: ["5432:5432"]
    healthcheck: ...

  api-gateway:
    build: ./services/api-gateway
    ports: ["8080:8080"]
    depends_on: [order-service, payment-service, inventory-service]

  order-service:
    build: ./services/order-service
    environment:
      - DB_HOST=postgres
      - PAYMENT_URL=http://payment-service:5000
      - INVENTORY_URL=http://inventory-service:5000

  payment-service:
    build: ./services/payment-service

  inventory-service:
    build: ./services/inventory-service
```

#### 故障场景

每个场景 YAML 定义：
- **正常行为**：服务间调用链、预期日志模式
- **故障注入方式**：环境变量、API 端点或启动参数
- **故障表现**：错误日志、响应延迟、服务不可用
- **预期诊断路径**：Agent 应该发现的日志线索和根因

复用 `skills/` 目录中已有的场景定义，适配为 Docker Compose 环境。

#### 服务设计原则

- 每个服务用 Python Flask 编写，保持简单
- 服务间通过 HTTP 调用（模拟微服务间通信）
- 故障注入通过环境变量控制（如 `FAULT_DB_POOL=true`）
- 每个服务输出结构化日志到 stdout（Docker 收集）
- PostgreSQL 使用连接池，可模拟连接池耗尽

## 5. 路由集成

### Web UI 路由

Web UI 路由作为单独的 router 挂载到 FastAPI app：

```python
# main.py 中新增
from incidentlens_control_plane.web.routes import router as web_router
application.include_router(web_router)
```

Web UI 路由前缀为 `/web`，与 API 路由（`/api`）分离。

### CLI 入口

CLI 作为独立的命令行入口点：

```toml
# pyproject.toml
[project.scripts]
incidentlens = "incidentlens_control_plane.cli.app:main"
```

`main()` 函数调用 `build_runtime()` 构建服务，然后启动 Textual App。

## 6. 配置扩展

`RuntimeSettings` 新增：

```python
# 报告输出目录（默认 data_dir / reports）
report_output_dir: Path | None = None

# Web UI 静态文件目录（默认由包内 static/ 提供）
web_static_dir: Path | None = None
```

## 7. 测试策略

### 单元测试

| 模块 | 测试文件 | 覆盖内容 |
|---|---|---|
| reports/ | tests/reports/test_markdown.py | Markdown 渲染、空调查、证据引用 |
| reports/ | tests/reports/test_html.py | HTML 渲染、diff 高亮、打印样式 |
| reports/ | tests/reports/test_service.py | 报告生成流程、缺失数据处理 |
| cli/ | tests/cli/test_screens.py | 屏幕渲染、状态显示 |
| cli/ | tests/cli/test_navigation.py | 键盘交互、屏幕切换 |
| web/ | tests/web/test_web_pages.py | 页面渲染、HTMX 响应 |
| web/ | tests/web/test_web_approvals.py | 审批操作 |
| web/ | tests/web/test_web_reports.py | 报告页面渲染 |

### 集成测试

| 测试文件 | 覆盖内容 |
|---|---|
| tests/acceptance/test_e2e_investigation.py | 完整调查流程：创建 → 启动 → 工具调用 → 审批 → 完成 → 报告 |
| tests/acceptance/test_e2e_logs.py | 日志查询 + 订阅 + 搜索 |
| tests/acceptance/test_e2e_multi_service.py | 多服务故障场景诊断 |

### 验收测试

| 测试文件 | 覆盖内容 |
|---|---|
| tests/acceptance/test_docker_scenarios.py | Docker Compose 场景：故障注入 → Agent 诊断 → 报告生成 |

需要 Docker 环境，通过 `INCIDENTLENS_RUN_ACCEPTANCE=1` 环境变量启用。

## 8. 实施顺序

### 阶段 1：报告服务（reports/）

**依赖：** InvestigationStore, EvidenceStore, ChangeSetStore（已存在）
**产出：** ReportService, MarkdownRenderer, HtmlRenderer
**测试：** 单元测试，无需 Docker

### 阶段 2：Web UI（web/）

**依赖：** 报告服务（HTML 输出）、现有 REST API
**产出：** 所有 Web 页面、SSE 事件流、Pico CSS 样式
**测试：** HTTP 测试 + 模板渲染测试

### 阶段 3：CLI（cli/）

**依赖：** RuntimeServices（直接导入）
**产出：** Textual App、6 个屏幕、2 个自定义组件
**测试：** 屏幕渲染测试、键盘交互测试

### 阶段 4：验收环境（infra/acceptance/）

**依赖：** 无（独立于 UI）
**产出：** docker-compose.yml、4 个模拟服务、4 个故障场景
**测试：** 手动验证 + 自动化场景测试

### 阶段 5：端到端验收（tests/acceptance/）

**依赖：** 以上全部
**产出：** E2E 测试套件、验收文档
**测试：** 完整流程验证

## 9. MVP 验收标准对照

| # | 设计规格标准 | Phase 5 覆盖 |
|---|---|---|
| 1 | 注册 Docker Compose 服务器和本地源码路径 | Web UI 项目管理页面 + CLI 项目命令 |
| 2 | CLI 发起调查 + Web UI 实时查看 | CLI Textual App + Web UI SSE 实时同步 |
| 3 | 按服务查询与持续采集日志 | Web UI 日志浏览器 + CLI 日志视图 |
| 4 | 查看错误/警告/关键正常日志 | 日志级别过滤 UI |
| 5 | 父 Agent 创建容器子 Agent | CLI/Web UI 时间线展示子 Agent |
| 6 | 持久 SSH 读取/搜索/编辑远程文件 | 工具调用在 UI 中可见 |
| 7 | 远程覆盖写入前双重备份 | 变更面板展示备份状态 |
| 8 | 阻止 rm -rf，服务操作需审批 | 审批面板 |
| 9 | 修改后验证 + 回滚 | 变更面板展示验证/回滚 |
| 10 | 最终报告（根因、证据、diff、建议） | ReportService 生成 Markdown + HTML |

## 10. 范围边界

### 包含

- Jinja2 + HTMX Web UI（8 个页面）
- Rich/Textual CLI（6 个屏幕）
- Markdown + HTML 调查报告
- Docker Compose 微服务验收环境（4 个模拟服务 + 4 个故障场景）
- 端到端验收测试
- Pico CSS 样式

### 不包含

- 用户认证/多用户支持（MVP 为本地单用户）
- 独立前端构建工具链（Vite/Webpack）
- 付费/云版本功能
- Kubernetes 部署
- 移动端适配
- 国际化

## 11. 风险与缓解

| 风险 | 缓解 |
|---|---|
| CLI 和 Web UI 并发写入 SQLite | MVP 中二选一使用；后续 WAL + 文件锁 |
| Textual 版本兼容性 | 锁定 Textual >=0.40,<1.0 |
| Docker Compose 环境启动慢 | 开发时使用 mock provider，仅验收时用 Docker |
| HTML 报告在不同浏览器兼容性 | 使用 Pico CSS + 标准 HTML/CSS，避免 JS |
| Pico CSS 样式不够定制 | CSS 变量覆盖，必要时补充自定义样式 |
