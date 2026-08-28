# Hybrid Log Evidence Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 FastAPI、SQLite、AsyncSSH 模块化单体中加入安全的混合日志查询、持久 opt-in 订阅、脱敏日志索引和只追加 Evidence Store。

**Architecture:** Phase 3 在 `runtime.py` 的服务容器内新增 logs/evidence 子系统，所有日志源共享“解析 -> 脱敏 -> 信号 -> 关联 -> 幂等键 -> 可选持久化/证据”的处理管线。远端访问只通过已注册 project/target/service 的固定 typed 操作，不接受客户端提供 SSH、host、credential、任意 Docker flags 或 shell 字符串。SQLite 使用同一个 `runtime.db` 执行幂等 migration，日志记录、FTS、订阅、游标和证据由 focused stores 管理。

**Tech Stack:** Python `>=3.12,<3.13`, FastAPI `>=0.115,<1`, Pydantic `>=2.13,<3`, AsyncSSH `>=2.24,<3`, stdlib `asyncio`/`sqlite3`/`hashlib`/`json`/`re`, pytest 8, pytest-asyncio, Ruff.

## Global Constraints

- 不增加远端 agent、独立 worker 或消息队列。
- 不实现多节点控制面协调、分布式租约或横向扩展。
- 不实现模型供应商接入、AgentRuntime、Web 前端或自动修复。
- 不允许任意 shell、任意 Docker 参数或客户端提供 SSH 连接信息。
- 不持久化原始日志；SQLite、FTS、Evidence Store、runtime events、HTTP/WebSocket 响应、错误详情和应用日志只能包含脱敏内容或安全摘要。
- 单条脱敏文本上限为 16 KiB，超出后截断并记录截断标记。
- `LogSubscription` 持续采集必须显式 `opt_in_streaming=true`。
- 只有 `active` 且 opt-in 的订阅在应用启动时恢复。
- 每个订阅队列 `asyncio.Queue(maxsize=1000)`，每批最多写入 100 条记录，默认同时 active 订阅上限为 20。
- Docker 日志按需 argv 固定为 `docker logs --timestamps --tail <bounded> -- <container>`。
- Docker 日志流式 argv 固定为 `docker logs --timestamps --follow --since <cursor-time> -- <container>`。
- Container list/search 只使用固定 argv 模板，不生成 shell 字符串，不允许用户控制命令参数。
- Evidence Store 只追加、不可修改；哈希基于脱敏内容计算。
- API 请求模型使用 `extra="forbid"`。
- 不新增 ORM、任务队列、远端 daemon 或长期归档组件。
- 项目命令使用 `UV_CACHE_DIR=.uv-cache uv run pytest ...` 和 `UV_CACHE_DIR=.uv-cache uv run ruff check ...`。

---

## File Structure

```text
apps/control-plane/src/incidentlens_control_plane/
  config.py                         增加日志订阅上限、批次、队列、轮询、Docker tail 等 runtime settings
  runtime.py                        构造并暴露 LogStore、EvidenceStore、LogService、LogSubscriptionManager
  main.py                           lifespan 恢复 active opt-in subscriptions；shutdown 先关闭订阅再关闭 SSH sessions

  project_registry/types.py         ServiceRegistration 日志路径白名单校验与兼容
  events/types.py                   增加 log.* runtime event 类型
  remote_ops/files.py               容器 list/search 后端和现有 RemoteFileTools 扩展
  remote_ops/gateway.py             container list/search 走注册容器和 RemotePathPolicy
  remote_ops/fakes.py               测试 fake transport 记录固定 Docker argv、模拟 docker logs/list/search

  logs/
    __init__.py                     logs 包导出
    types.py                        LogSourceKind、LogScope、RawLogLine、LogQueryRequest、LogRecord、subscription 模型
    parser.py                       timestamp、severity、JSON/text 结构解析
    redaction.py                    敏感信息脱敏和 16 KiB 截断
    signals.py                      deterministic normal signal 标签
    correlation.py                  trace/request/span/correlation key 提取
    sources.py                      file/docker 按需和 stream source；固定 argv 与 cursor
    store.py                        log_records、FTS5、log_subscriptions、log_cursors、runs migration 和 repository
    service.py                      注册边界解析、处理管线、query/search、证据协调
    subscriptions.py                订阅 task、queue、batch writer、恢复、pause/resume、重试、背压

  evidence/
    __init__.py                     evidence 包导出
    types.py                        EvidenceRef、CreateEvidenceFromRecordsRequest 等模型
    store.py                        evidence_refs migration、append-only 幂等创建和查询

  routes/
    logs.py                         /api/logs/query/search/subscriptions/.../ws
    evidence.py                     /api/evidence/from-log-records、/api/evidence/{id}、/api/incidents/{id}/evidence

tests/
  logs/
    test_parser.py
    test_redaction.py
    test_signals.py
    test_correlation.py
    test_store.py
    test_sources_file.py
    test_sources_docker.py
    test_service.py
    test_subscriptions.py
  evidence/
    test_store.py
  remote_ops/
    test_files.py                   扩展 container list/search
    test_gateway.py                 扩展 container gateway authorization
  web/
    test_logs_api.py
    test_evidence_api.py
    test_log_subscriptions_api.py
  integration/
    test_live_log_tools.py          opt-in，默认 skip
docs/
  phase-3-hybrid-log-evidence-verification.md
README.md
```

## File Responsibility Map

- `logs/types.py`：所有 logs 子系统的不可变 Pydantic/domain 类型。这里定义的字段名是 DB、service、routes、tests 的唯一契约。
- `logs/parser.py`：只负责从单行文本解析 timestamp、severity 和可保留结构化字段；JSON 失败必须回退文本解析。
- `logs/redaction.py`：唯一允许处理原始日志敏感内容的模块；输出脱敏文本与 summary，任何 store/event/API 只能使用输出值。
- `logs/signals.py` 与 `logs/correlation.py`：纯函数、无 I/O，用确定性规则生成 normal signal 与 correlation key。
- `logs/sources.py`：只负责从注册边界和 fixed argv 读取 raw line/cursor；不持久化、不发布事件。
- `logs/store.py`：SQLite schema、migration、事务、FTS5、幂等 dedupe、订阅/游标/runs repository。
- `logs/service.py`：注册表解析、路径和容器授权、处理管线编排、query/search 业务规则、可选 evidence 创建。
- `logs/subscriptions.py`：订阅生命周期、任务、队列、批写入、cursor 推进、恢复、pause/resume、背压、重试、安全事件。
- `evidence/store.py`：只追加证据库；不接受 raw text，不提供 update/delete。
- `routes/logs.py` 与 `routes/evidence.py`：HTTP/WebSocket 模型、错误码映射、response shape；不包含远端 I/O 细节。
- `runtime.py` 与 `main.py`：服务构造和 lifecycle 顺序；不得把订阅恢复散落到 route 中。
- `remote_ops/files.py` 与 `remote_ops/gateway.py`：补齐 Phase 2 container list/search，供 logs source 复用固定 Docker exec 能力。

---

### Task 1: 补齐 Container List/Search 和 Fake Docker argv

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/files.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/gateway.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/fakes.py`
- Modify: `tests/remote_ops/test_files.py`
- Modify: `tests/remote_ops/test_gateway.py`

**Interfaces:**
- Consumes: `ContainerFileBackend.read_bytes(path, max_bytes)`, `ContainerFileBackend.lstat(path)`, `RemoteFileTools.search()`, `RemotePathPolicy.authorize(scope, path, write=False)`.
- Produces:
  - `ContainerFileBackend.list_directory(path: PurePosixPath) -> tuple[FileMetadata, ...]`
  - `ContainerFileBackend.search(path: PurePosixPath, query: str) -> tuple[SearchMatch, ...]`
  - `RemoteToolGateway.list_dir(..., scope={"kind": "container", "container": str}) -> tuple[FileMetadata, ...]`
  - `RemoteToolGateway.search(..., scope={"kind": "container", "container": str}) -> tuple[SearchMatch, ...]`

- [ ] **Step 1: Write failing container backend tests**

Add to `tests/remote_ops/test_files.py`:

```python
@pytest.mark.asyncio
async def test_container_backend_lists_directory_with_fixed_argv() -> None:
    from incidentlens_control_plane.remote_ops.fakes import FakeChangeTransport
    from incidentlens_control_plane.remote_ops.files import ContainerFileBackend

    transport = FakeChangeTransport()
    transport.container_files[PurePosixPath("/app/app.py")] = b"print('ok')\n"
    transport.container_files[PurePosixPath("/app/secret.log")] = b"token=abc\n"

    backend = ContainerFileBackend(transport, "payments-api-1")
    entries = await backend.list_directory(PurePosixPath("/app"))

    assert {entry.path for entry in entries} == {
        PurePosixPath("/app/app.py"),
        PurePosixPath("/app/secret.log"),
    }
    assert transport.run_argv_calls == [
        (
            "docker",
            "exec",
            "payments-api-1",
            "find",
            "/app",
            "-maxdepth",
            "1",
            "-mindepth",
            "1",
            "-printf",
            "%p|%y|%s|%m|%u|%g|%T@\n",
        )
    ]


@pytest.mark.asyncio
async def test_container_backend_search_skips_symlinks_and_caps_matches() -> None:
    from incidentlens_control_plane.remote_ops.fakes import FakeChangeTransport
    from incidentlens_control_plane.remote_ops.files import ContainerFileBackend

    transport = FakeChangeTransport()
    for index in range(250):
        transport.container_files[PurePosixPath(f"/app/file-{index}.log")] = (
            f"line {index} token=abc\n".encode()
        )
    transport.container_symlinks.add(PurePosixPath("/app/link.log"))

    backend = ContainerFileBackend(transport, "payments-api-1")
    matches = await backend.search(PurePosixPath("/app"), "token")

    assert len(matches) == 200
    assert all(match.path.name != "link.log" for match in matches)
    assert all(match.text.endswith("token=abc") for match in matches)
```

- [ ] **Step 2: Write failing gateway authorization tests**

Add to `tests/remote_ops/test_gateway.py`:

```python
@pytest.mark.asyncio
async def test_gateway_container_list_requires_registered_container(
    project_store, target_registration, service_registration
) -> None:
    from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
    from incidentlens_control_plane.remote_ops.gateway import RemoteToolGateway
    from incidentlens_control_plane.remote_ops.sessions import SessionManager

    project_store.create(
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            targets=(target_registration,),
            services=(service_registration,),
        ),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    gateway = RemoteToolGateway(
        projects=project_store,
        sessions=SessionManager(FakeTransportFactory()),
    )

    with pytest.raises(Exception, match="unknown container|not registered"):
        await gateway.list_dir(
            project_id="payments",
            target_id="dev-a",
            service="payment-api",
            path=PurePosixPath("/app"),
            scope={"kind": "container", "container": "attacker"},
        )


@pytest.mark.asyncio
async def test_gateway_container_search_rejects_path_outside_allowed_root(
    project_store, target_registration, service_registration
) -> None:
    from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
    from incidentlens_control_plane.remote_ops.gateway import RemoteToolGateway
    from incidentlens_control_plane.remote_ops.sessions import SessionManager

    project_store.create(
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            targets=(target_registration,),
            services=(service_registration,),
        ),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    gateway = RemoteToolGateway(
        projects=project_store,
        sessions=SessionManager(FakeTransportFactory()),
    )

    with pytest.raises(Exception, match="outside allowed roots"):
        await gateway.search(
            project_id="payments",
            target_id="dev-a",
            service="payment-api",
            path=PurePosixPath("/etc"),
            query="token",
            scope={"kind": "container", "container": "payments-api-1"},
        )
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/remote_ops/test_files.py::test_container_backend_lists_directory_with_fixed_argv tests/remote_ops/test_files.py::test_container_backend_search_skips_symlinks_and_caps_matches tests/remote_ops/test_gateway.py::test_gateway_container_list_requires_registered_container tests/remote_ops/test_gateway.py::test_gateway_container_search_rejects_path_outside_allowed_root -q
```

Expected: FAIL because `ContainerFileBackend.list_directory()` and `ContainerFileBackend.search()` do not exist, and gateway still raises `"container directory listing is not supported"` / `"container search is not supported"`.

- [ ] **Step 4: Implement minimal container list/search**

Add to `ContainerFileBackend`:

```python
async def list_directory(self, path: PurePosixPath) -> tuple[FileMetadata, ...]:
    result = await self._run(
        (
            "docker",
            "exec",
            self._container,
            "find",
            str(path),
            "-maxdepth",
            "1",
            "-mindepth",
            "1",
            "-printf",
            "%p|%y|%s|%m|%u|%g|%T@\n",
        ),
        timeout=30.0,
    )
    entries: list[FileMetadata] = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        fields = line.split("|")
        if len(fields) != 7:
            raise ContainerFileOperationUnsupported("unparseable find output")
        entry_path, file_type, size_s, mode_s, uid_s, gid_s, mtime_s = fields
        entries.append(
            FileMetadata(
                path=PurePosixPath(entry_path),
                size=int(size_s),
                mode=int(mode_s, 8),
                uid=int(uid_s) if uid_s.isdigit() else 0,
                gid=int(gid_s) if gid_s.isdigit() else 0,
                modified_ns=int(float(mtime_s) * 1_000_000_000),
                is_symlink=file_type == "l",
            )
        )
    return tuple(entries)


async def search(
    self,
    path: PurePosixPath,
    query: str,
) -> tuple[SearchMatch, ...]:
    return await RemoteFileTools(self).search(path, query)
```

Modify `RemoteToolGateway.list_dir()` container branch:

```python
if isinstance(resolved_scope, ContainerScope):
    canonical = await self._authorize_path(svc, resolved_scope, path, write=False)
    backend = await self._container_backend(target, resolved_scope)
    return await backend.list_directory(canonical)
```

Modify `RemoteToolGateway.search()` container branch:

```python
if isinstance(resolved_scope, ContainerScope):
    canonical = await self._authorize_path(svc, resolved_scope, path, write=False)
    backend = await self._container_backend(target, resolved_scope)
    return await backend.search(canonical, query)
```

Extend `FakeChangeTransport` with:

```python
container_symlinks: set[PurePosixPath] = field(default_factory=set)

def _container_find_result(self, root: PurePosixPath) -> CommandResult:
    lines: list[str] = []
    paths = sorted(set(self.container_files) | self.container_symlinks)
    for path in paths:
        if path.parent != root:
            continue
        if path in self.container_symlinks:
            lines.append(f"{path}|l|0|777|1000|1000|0")
        else:
            lines.append(f"{path}|f|{len(self.container_files[path])}|644|1000|1000|0")
    return CommandResult(exit_status=0, stdout=("\n".join(lines) + "\n").encode(), stderr=b"")
```

Call it from `_simulate_docker_argv()` when argv matches the fixed `find` template.

- [ ] **Step 5: Run tests and lint**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/remote_ops/test_files.py tests/remote_ops/test_gateway.py -q
```

Expected: PASS.

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/remote_ops tests/remote_ops
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/remote_ops/files.py apps/control-plane/src/incidentlens_control_plane/remote_ops/gateway.py apps/control-plane/src/incidentlens_control_plane/remote_ops/fakes.py tests/remote_ops/test_files.py tests/remote_ops/test_gateway.py
git commit -m "feat: support container list and search" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: 定义日志模型、解析、脱敏、信号和关联纯函数

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/logs/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/logs/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/logs/parser.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/logs/redaction.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/logs/signals.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/logs/correlation.py`
- Create: `tests/logs/test_parser.py`
- Create: `tests/logs/test_redaction.py`
- Create: `tests/logs/test_signals.py`
- Create: `tests/logs/test_correlation.py`

**Interfaces:**
- Consumes: no project services; pure stdlib/Pydantic only.
- Produces:
  - `LogSeverity(StrEnum)`
  - `LogSourceKind(StrEnum)` values `"file"`, `"docker"`
  - `LogScope(StrEnum)` values `"host"`, `"container"`
  - `RawLogLine(source_ref: str, cursor: str, observed_at: datetime, text: str)`
  - `ParsedLogLine(event_time: datetime | None, severity: LogSeverity, fields: dict[str, object], message: str)`
  - `RedactionResult(message_redacted: str, summary: dict[str, int], truncated: bool)`
  - `parse_log_line(text: str) -> ParsedLogLine`
  - `redact_message(message: str) -> RedactionResult`
  - `detect_normal_signal(parsed: ParsedLogLine) -> str | None`
  - `extract_correlation_key(parsed: ParsedLogLine) -> str | None`

- [ ] **Step 1: Write failing parser tests**

Create `tests/logs/test_parser.py`:

```python
from datetime import UTC, datetime

from incidentlens_control_plane.logs.parser import parse_log_line
from incidentlens_control_plane.logs.types import LogSeverity


def test_parse_json_severity_and_timestamp() -> None:
    parsed = parse_log_line(
        '{"timestamp":"2026-08-12T10:11:12Z","level":"ERROR","message":"failed"}'
    )

    assert parsed.event_time == datetime(2026, 8, 12, 10, 11, 12, tzinfo=UTC)
    assert parsed.severity is LogSeverity.ERROR
    assert parsed.message == "failed"
    assert parsed.fields["level"] == "ERROR"


def test_invalid_json_falls_back_to_text_severity() -> None:
    parsed = parse_log_line('{"level": "ERROR" broken warn fallback')

    assert parsed.event_time is None
    assert parsed.severity is LogSeverity.WARN
    assert parsed.message == '{"level": "ERROR" broken warn fallback'


def test_unknown_severity_when_no_token_matches() -> None:
    parsed = parse_log_line("service emitted an ordinary line")
    assert parsed.severity is LogSeverity.UNKNOWN
```

- [ ] **Step 2: Write failing redaction/signal/correlation tests**

Create `tests/logs/test_redaction.py`:

```python
from incidentlens_control_plane.logs.redaction import redact_message


def test_redacts_token_password_email_ip_and_url_secret() -> None:
    result = redact_message(
        "token=abc123 password=hunter2 user=a@example.com ip=10.1.2.3 "
        "https://api.example.test/callback?secret=s3cr3t"
    )

    assert "abc123" not in result.message_redacted
    assert "hunter2" not in result.message_redacted
    assert "a@example.com" not in result.message_redacted
    assert "10.1.2.3" not in result.message_redacted
    assert "s3cr3t" not in result.message_redacted
    assert result.summary["token"] == 1
    assert result.summary["password"] == 1
    assert result.summary["email"] == 1
    assert result.summary["ip"] == 1
    assert result.summary["url_secret"] == 1


def test_truncates_redacted_message_to_16_kib() -> None:
    result = redact_message("x" * (16 * 1024 + 10))

    assert len(result.message_redacted) <= 16 * 1024
    assert result.truncated is True
    assert result.summary["truncated"] == 1
```

Create `tests/logs/test_signals.py`:

```python
from incidentlens_control_plane.logs.parser import parse_log_line
from incidentlens_control_plane.logs.signals import detect_normal_signal


def test_detects_deterministic_normal_signals() -> None:
    assert detect_normal_signal(parse_log_line("heartbeat ok")) == "heartbeat"
    assert detect_normal_signal(parse_log_line("GET /health 200 OK")) == "healthcheck_ok"
    assert detect_normal_signal(parse_log_line("GET /api/payments 200 31ms")) == "request_ok"
    assert detect_normal_signal(parse_log_line("service startup complete")) == "startup"
    assert detect_normal_signal(parse_log_line("shutdown requested")) == "shutdown"
    assert detect_normal_signal(parse_log_line("retrying connection attempt 2")) == "retry"
    assert detect_normal_signal(parse_log_line("unexpected payment failure")) is None
```

Create `tests/logs/test_correlation.py`:

```python
from incidentlens_control_plane.logs.correlation import extract_correlation_key
from incidentlens_control_plane.logs.parser import parse_log_line


def test_extracts_trace_then_request_then_span_then_correlation_id() -> None:
    assert extract_correlation_key(parse_log_line('{"trace_id":"tr-1","request_id":"req-1"}')) == "trace:tr-1"
    assert extract_correlation_key(parse_log_line("request_id=req-2 span_id=sp-2")) == "request:req-2"
    assert extract_correlation_key(parse_log_line("span=sp-3")) == "span:sp-3"
    assert extract_correlation_key(parse_log_line("correlation_id=corr-4")) == "correlation:corr-4"


def test_does_not_generate_fake_service_only_correlation() -> None:
    assert extract_correlation_key(parse_log_line("payment-api container started")) is None
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/logs/test_parser.py tests/logs/test_redaction.py tests/logs/test_signals.py tests/logs/test_correlation.py -q
```

Expected: FAIL because `incidentlens_control_plane.logs` package does not exist.

- [ ] **Step 4: Implement minimal types and pure functions**

In `logs/types.py`:

```python
from datetime import datetime
from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field


class LogSeverity(StrEnum):
    TRACE = "trace"
    DEBUG = "debug"
    INFO = "info"
    NOTICE = "notice"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class LogSourceKind(StrEnum):
    FILE = "file"
    DOCKER = "docker"


class LogScope(StrEnum):
    HOST = "host"
    CONTAINER = "container"


class RawLogLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_ref: str = Field(min_length=1, max_length=500)
    cursor: str = Field(min_length=1, max_length=1000)
    observed_at: datetime
    text: str


class ParsedLogLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_time: datetime | None
    severity: LogSeverity
    fields: dict[str, object]
    message: str


class RedactionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    message_redacted: str
    summary: dict[str, int]
    truncated: bool = False
```

In `logs/parser.py`, implement JSON-first then text fallback:

```python
_JSON_SEVERITY_KEYS = ("severity", "level", "log.level", "lvl")
_SEVERITY_ALIASES = {
    "trace": LogSeverity.TRACE,
    "debug": LogSeverity.DEBUG,
    "info": LogSeverity.INFO,
    "notice": LogSeverity.NOTICE,
    "warn": LogSeverity.WARN,
    "warning": LogSeverity.WARN,
    "error": LogSeverity.ERROR,
    "err": LogSeverity.ERROR,
    "critical": LogSeverity.CRITICAL,
    "crit": LogSeverity.CRITICAL,
    "fatal": LogSeverity.CRITICAL,
}

def parse_log_line(text: str) -> ParsedLogLine:
    fields: dict[str, object] = {}
    message = text
    event_time: datetime | None = None
    severity = LogSeverity.UNKNOWN
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None

    if isinstance(value, dict):
        fields = dict(value)
        message_value = value.get("message") or value.get("msg") or text
        message = str(message_value)
        for key in ("timestamp", "time", "@timestamp", "ts"):
            if key in value:
                event_time = _parse_time(str(value[key]))
                break
        for key in _JSON_SEVERITY_KEYS:
            if key in value:
                severity = _severity_from_token(str(value[key]))
                break

    if severity is LogSeverity.UNKNOWN:
        severity = _severity_from_text(text)
    if event_time is None:
        event_time = _parse_time(text)
    return ParsedLogLine(
        event_time=event_time,
        severity=severity,
        fields=fields,
        message=message,
    )
```

In `redaction.py`, apply deterministic regex replacements for token/password/private key/URL secrets/email/IP before truncation.

In `signals.py`, use lowercase regexes for heartbeat, healthcheck 200, request 2xx, startup, shutdown, retry.

In `correlation.py`, first inspect structured fields, then text regexes, in order trace/request/span/correlation.

- [ ] **Step 5: Run tests and lint**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/logs/test_parser.py tests/logs/test_redaction.py tests/logs/test_signals.py tests/logs/test_correlation.py -q
```

Expected: PASS.

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/logs tests/logs/test_parser.py tests/logs/test_redaction.py tests/logs/test_signals.py tests/logs/test_correlation.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/logs tests/logs/test_parser.py tests/logs/test_redaction.py tests/logs/test_signals.py tests/logs/test_correlation.py
git commit -m "feat: add log parsing and redaction primitives" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: 实现 LogStore、FTS5、订阅表、游标和 runs

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/logs/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/logs/store.py`
- Create: `tests/logs/test_store.py`

**Interfaces:**
- Consumes: `LogSeverity`, `LogSourceKind`, `LogScope`, `RedactionResult`.
- Produces:
  - `LogRecord(...)`
  - `LogSearchFilters(...)`
  - `LogSubscriptionStatus(StrEnum)` values `"active"`, `"paused"`, `"error"`, `"deleted"`
  - `LogSubscription(...)`
  - `LogCursor(subscription_id: str, cursor: str, generation: str | None, observed_at: datetime | None, updated_at: datetime)`
  - `LogStore.migrate() -> None`
  - `LogStore.append_batch(records: tuple[LogRecord, ...]) -> tuple[LogRecord, ...]`
  - `LogStore.search(filters: LogSearchFilters, limit: int = 100) -> tuple[LogRecord, ...]`
  - subscription/cursor/runs CRUD methods used by later tasks.

- [ ] **Step 1: Write failing store tests**

Create `tests/logs/test_store.py`:

```python
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from incidentlens_control_plane.logs.store import LogSearchFilters, LogStore
from incidentlens_control_plane.logs.types import LogRecord, LogSeverity, LogSourceKind, LogScope


def make_store(tmp_path: Path) -> LogStore:
    store = LogStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    return store


def make_record(message: str, *, dedupe_key: str = "dedupe-1") -> LogRecord:
    return LogRecord(
        log_id="log-1",
        subscription_id=None,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        cursor="offset:1",
        dedupe_key=dedupe_key,
        observed_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        event_time=None,
        severity=LogSeverity.ERROR,
        message_redacted=message,
        redaction_summary={"token": 1},
        normal_signal=None,
        correlation_key="trace:abc",
        evidence_ref_id=None,
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


def test_append_batch_deduplicates_by_dedupe_key(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = make_record("[REDACTED_TOKEN] failed")
    duplicate = first.model_copy(update={"log_id": "log-2"})

    inserted = store.append_batch((first, duplicate))

    assert inserted == (first,)
    assert store.search(LogSearchFilters(project_id="payments"), limit=10) == (first,)


def test_fts_search_indexes_only_redacted_message(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append_batch((make_record("[REDACTED_TOKEN] database failed"),))

    results = store.search(LogSearchFilters(project_id="payments", text="database"), limit=10)

    assert len(results) == 1
    assert "abc123" not in results[0].model_dump_json()
    assert results[0].message_redacted == "[REDACTED_TOKEN] database failed"


def test_schema_has_no_raw_message_column(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.migrate()
    with sqlite3.connect(tmp_path / "runtime.db") as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(log_records)").fetchall()
        }

    assert "raw_message" not in columns
    assert "message_redacted" in columns
```

- [ ] **Step 2: Add subscription/cursor tests**

Add:

```python
def test_active_opt_in_subscriptions_are_listed_for_restore(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    active = store.create_subscription(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    paused = store.pause_subscription(active.subscription_id, now=datetime(2026, 8, 12, tzinfo=UTC))

    assert paused.status.value == "paused"
    assert store.list_active_opt_in_subscriptions() == ()

    resumed = store.resume_subscription(active.subscription_id, now=datetime(2026, 8, 12, tzinfo=UTC))
    assert resumed.status.value == "active"
    assert store.list_active_opt_in_subscriptions() == (resumed,)


def test_cursor_upsert_round_trips(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    now = datetime(2026, 8, 12, tzinfo=UTC)

    store.upsert_cursor(
        subscription_id="sub-1",
        cursor="offset:42",
        generation="mtime=1:size=42",
        observed_at=now,
        now=now,
    )

    cursor = store.get_cursor("sub-1")
    assert cursor is not None
    assert cursor.cursor == "offset:42"
    assert cursor.generation == "mtime=1:size=42"
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/logs/test_store.py -q
```

Expected: FAIL because `logs.store.LogStore`, `LogRecord`, and subscription types do not exist.

- [ ] **Step 4: Implement schema and repository**

Add to `logs/types.py`:

```python
class LogRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    log_id: str = Field(min_length=1, max_length=120)
    subscription_id: str | None = Field(default=None, max_length=120)
    project_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)
    service_name: str = Field(min_length=1, max_length=120)
    source_kind: LogSourceKind
    scope: LogScope
    source_ref: str = Field(min_length=1, max_length=500)
    cursor: str = Field(min_length=1, max_length=1000)
    dedupe_key: str = Field(min_length=1, max_length=128)
    observed_at: datetime
    event_time: datetime | None
    severity: LogSeverity
    message_redacted: str = Field(max_length=16 * 1024)
    redaction_summary: dict[str, int]
    normal_signal: str | None = None
    correlation_key: str | None = None
    evidence_ref_id: str | None = None
    created_at: datetime
```

In `logs/store.py`, create tables:

```sql
CREATE TABLE IF NOT EXISTS log_records (
    log_id TEXT PRIMARY KEY,
    subscription_id TEXT,
    project_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    service_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    scope TEXT NOT NULL,
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
    evidence_ref_id TEXT,
    created_at TEXT NOT NULL
)
```

Also create:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS log_records_fts
USING fts5(log_id UNINDEXED, message_redacted);

CREATE TABLE IF NOT EXISTS log_subscriptions (...);
CREATE TABLE IF NOT EXISTS log_cursors (...);
CREATE TABLE IF NOT EXISTS log_subscription_runs (...);
```

`append_batch()` must wrap `log_records` and `log_records_fts` writes in one connection context and use `INSERT OR IGNORE`. Insert FTS only when the record insert rowcount is 1.

Use a safe token search conversion:

```python
def _fts_query(text: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_./:-]+", text)
    if not tokens:
        raise ValueError("search text must contain at least one token")
    return " ".join(f'"{token}"' for token in tokens[:8])
```

- [ ] **Step 5: Run tests and lint**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/logs/test_store.py -q
```

Expected: PASS.

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/logs tests/logs/test_store.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/logs/types.py apps/control-plane/src/incidentlens_control_plane/logs/store.py tests/logs/test_store.py
git commit -m "feat: persist redacted log records and subscriptions" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: 实现按需 File/Docker Log Sources 和处理管线 Service

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/logs/sources.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/logs/service.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/logs/types.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/fakes.py`
- Create: `tests/logs/test_sources_file.py`
- Create: `tests/logs/test_sources_docker.py`
- Create: `tests/logs/test_service.py`

**Interfaces:**
- Consumes: Task 2 parser/redaction/signal/correlation, Task 3 `LogStore`, `ProjectRegistryStore`, `SessionManager`, `RemotePathPolicy`.
- Produces:
  - `LogQueryRequest(...)`
  - `ProcessedLogLine(...)`
  - `LogService.query(request: LogQueryRequest, *, now: datetime) -> tuple[LogRecord, ...]`
  - `FileLogSource.query(request) -> tuple[RawLogLine, ...]`
  - `DockerLogSource.query(request) -> tuple[RawLogLine, ...]`

- [ ] **Step 1: Write failing file source tests**

Create `tests/logs/test_sources_file.py`:

```python
@pytest.mark.asyncio
async def test_file_log_source_reads_tail_without_loading_full_file(target_registration) -> None:
    from incidentlens_control_plane.logs.sources import FileLogSource
    from incidentlens_control_plane.logs.types import LogQueryRequest, LogScope, LogSourceKind
    from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
    from incidentlens_control_plane.remote_ops.sessions import SessionManager

    factory = FakeTransportFactory()
    session = await SessionManager(factory).connect(target_registration)
    session.transport._files[PurePosixPath("/var/log/payment/app.log")] = (
        b"old\n" + b"x" * 10_000 + b"\nERROR token=abc\n"
    )
    source = FileLogSource(SessionManager(factory))

    request = LogQueryRequest(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        tail_lines=1,
        persist=False,
        create_evidence=False,
    )

    lines = await source.query(request, target_registration, PurePosixPath("/var/log/payment/app.log"))

    assert len(lines) == 1
    assert lines[0].text == "ERROR token=abc"
    assert lines[0].cursor.startswith("file:")
```

- [ ] **Step 2: Write failing Docker source argv tests**

Create `tests/logs/test_sources_docker.py`:

```python
@pytest.mark.asyncio
async def test_docker_log_source_uses_fixed_bounded_logs_argv(target_registration) -> None:
    from incidentlens_control_plane.logs.sources import DockerLogSource
    from incidentlens_control_plane.logs.types import LogQueryRequest, LogScope, LogSourceKind
    from incidentlens_control_plane.remote_ops.fakes import FakeChangeTransport

    transport = FakeChangeTransport()
    transport.docker_logs[("payments-api-1", 50)] = (
        b"2026-08-12T10:00:00Z ERROR token=abc\n"
    )
    source = DockerLogSource(lambda target: transport)

    request = LogQueryRequest(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.DOCKER,
        scope=LogScope.CONTAINER,
        source_ref="payments-api-1",
        tail_lines=50,
        persist=False,
        create_evidence=False,
    )

    lines = await source.query(request, target_registration)

    assert lines[0].text.endswith("ERROR token=abc")
    assert transport.run_argv_calls == [
        (
            "docker",
            "logs",
            "--timestamps",
            "--tail",
            "50",
            "--",
            "payments-api-1",
        )
    ]
```

- [ ] **Step 3: Write failing service pipeline tests**

Create `tests/logs/test_service.py`:

```python
@pytest.mark.asyncio
async def test_log_service_query_redacts_before_persisting(
    tmp_path, target_registration
) -> None:
    store = LogStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    projects = ProjectRegistryStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    projects.migrate()
    projects.create(
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            targets=(target_registration,),
            services=(
                ServiceRegistration(
                    compose_service="payment-api",
                    container_names=("payments-api-1",),
                    allowed_log_paths=("/var/log/payment/app.log",),
                    allowed_host_paths=(PurePosixPath("/var/log/payment"),),
                ),
            ),
        ),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    service = LogService(projects=projects, store=store, sessions=SessionManager(FakeTransportFactory()))
    session = await service._sessions.connect(target_registration)
    session.transport._files[PurePosixPath("/var/log/payment/app.log")] = b"ERROR token=abc123\n"

    records = await service.query(
        LogQueryRequest(
            project_id="payments",
            target_id="dev-a",
            service_name="payment-api",
            source_kind=LogSourceKind.FILE,
            scope=LogScope.HOST,
            source_ref="/var/log/payment/app.log",
            tail_lines=10,
            persist=True,
            create_evidence=False,
        ),
        now=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )

    assert len(records) == 1
    assert "abc123" not in records[0].message_redacted
    assert store.search(LogSearchFilters(project_id="payments", text="ERROR"), limit=10) == records
```

- [ ] **Step 4: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/logs/test_sources_file.py tests/logs/test_sources_docker.py tests/logs/test_service.py -q
```

Expected: FAIL because `logs.sources`, `logs.service`, and `LogQueryRequest` do not exist.

- [ ] **Step 5: Implement sources and service**

Add to `logs/types.py`:

```python
class LogQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)
    service_name: str = Field(min_length=1, max_length=120)
    source_kind: LogSourceKind
    scope: LogScope
    source_ref: str = Field(min_length=1, max_length=500)
    tail_lines: int = Field(default=100, ge=1, le=1000)
    persist: bool = False
    create_evidence: bool = False
    incident_id: str | None = Field(default=None, max_length=120)
```

Implement `DockerLogSource.query()` fixed argv exactly:

```python
tail = str(min(max(request.tail_lines, 1), 1000))
result = await transport.run_argv(
    ("docker", "logs", "--timestamps", "--tail", tail, "--", request.source_ref),
    timeout=30.0,
)
if result.exit_status != 0:
    raise LogSourceUnavailable("docker logs failed")
```

Implement `LogService.query()`:
1. Resolve project, target, service.
2. For file source, require `source_ref` path allowed by `allowed_log_paths` if non-empty, else `allowed_host_paths` / `allowed_container_paths`.
3. For Docker source, require `source_ref in service.container_names`.
4. Run source query.
5. For each raw line, call parser, redaction, signal, correlation.
6. Compute dedupe key from `project_id|target_id|service|source_kind|scope|source_ref|cursor|message_redacted`.
7. Return `LogRecord` objects; if `persist=True`, insert with `store.append_batch()` and return inserted/existing records consistently by querying store.

- [ ] **Step 6: Run tests and lint**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/logs/test_sources_file.py tests/logs/test_sources_docker.py tests/logs/test_service.py -q
```

Expected: PASS.

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/logs apps/control-plane/src/incidentlens_control_plane/remote_ops/fakes.py tests/logs
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/logs apps/control-plane/src/incidentlens_control_plane/remote_ops/fakes.py tests/logs
git commit -m "feat: query file and docker logs safely" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: 实现 Evidence Store 和从日志记录创建证据

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/evidence/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/evidence/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/evidence/store.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/logs/service.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/logs/types.py`
- Create: `tests/evidence/test_store.py`
- Modify: `tests/logs/test_service.py`

**Interfaces:**
- Consumes: `LogRecord` whose `message_redacted` is already processed.
- Produces:
  - `EvidenceKind(StrEnum)` with `"log_record"`
  - `EvidenceRef(...)`
  - `EvidenceStore.migrate() -> None`
  - `EvidenceStore.create_from_log_record(record: LogRecord, incident_id: str, created_by: str, now: datetime) -> EvidenceRef`
  - `EvidenceStore.get(evidence_ref_id: str) -> EvidenceRef`
  - `EvidenceStore.list_for_incident(incident_id: str, limit: int = 100) -> tuple[EvidenceRef, ...]`

- [ ] **Step 1: Write failing evidence store tests**

Create `tests/evidence/test_store.py`:

```python
import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.logs.types import LogRecord, LogScope, LogSeverity, LogSourceKind


def make_log_record() -> LogRecord:
    return LogRecord(
        log_id="log-1",
        subscription_id=None,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        cursor="offset:1",
        dedupe_key="dedupe-1",
        observed_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        event_time=None,
        severity=LogSeverity.ERROR,
        message_redacted="ERROR token=[REDACTED_TOKEN]",
        redaction_summary={"token": 1},
        normal_signal=None,
        correlation_key="trace:abc",
        evidence_ref_id=None,
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


def test_create_evidence_hashes_redacted_content(tmp_path: Path) -> None:
    store = EvidenceStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    record = make_log_record()

    evidence = store.create_from_log_record(
        record,
        incident_id="inc-1",
        created_by="alice",
        now=datetime(2026, 8, 12, 10, 1, tzinfo=UTC),
    )

    assert evidence.content_redacted == record.message_redacted
    assert evidence.content_sha256 == hashlib.sha256(record.message_redacted.encode()).hexdigest()
    assert "abc123" not in evidence.model_dump_json()


def test_create_evidence_is_idempotent_for_same_source_cursor_hash(tmp_path: Path) -> None:
    store = EvidenceStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    record = make_log_record()

    first = store.create_from_log_record(record, incident_id="inc-1", created_by="alice", now=datetime(2026, 8, 12, tzinfo=UTC))
    second = store.create_from_log_record(record, incident_id="inc-1", created_by="alice", now=datetime(2026, 8, 12, tzinfo=UTC))

    assert second.evidence_ref_id == first.evidence_ref_id
    assert store.list_for_incident("inc-1", limit=10) == (first,)


def test_evidence_schema_has_no_raw_content_column(tmp_path: Path) -> None:
    store = EvidenceStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    with sqlite3.connect(tmp_path / "runtime.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence_refs)")}

    assert "content_raw" not in columns
    assert "content_redacted" in columns
```

- [ ] **Step 2: Write failing LogService evidence integration test**

Add to `tests/logs/test_service.py`:

```python
@pytest.mark.asyncio
async def test_query_create_evidence_requires_incident_id(tmp_path, target_registration) -> None:
    service = build_test_log_service(tmp_path, target_registration)

    with pytest.raises(ValueError, match="incident_id is required"):
        await service.query(
            LogQueryRequest(
                project_id="payments",
                target_id="dev-a",
                service_name="payment-api",
                source_kind=LogSourceKind.FILE,
                scope=LogScope.HOST,
                source_ref="/var/log/payment/app.log",
                persist=True,
                create_evidence=True,
                incident_id=None,
            ),
            now=datetime(2026, 8, 12, tzinfo=UTC),
        )
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/evidence/test_store.py tests/logs/test_service.py::test_query_create_evidence_requires_incident_id -q
```

Expected: FAIL because evidence package does not exist and `LogService` has no evidence integration.

- [ ] **Step 4: Implement append-only evidence store**

In `evidence/types.py`:

```python
class EvidenceKind(StrEnum):
    LOG_RECORD = "log_record"


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence_ref_id: str
    incident_id: str
    evidence_kind: EvidenceKind
    project_id: str
    target_id: str
    service_name: str
    source_kind: LogSourceKind
    scope: LogScope
    source_ref: str
    cursor: str
    content_redacted: str
    content_sha256: str
    redaction_summary: dict[str, int]
    severity: LogSeverity
    event_time: datetime | None
    normal_signal: str | None
    correlation_key: str | None
    created_at: datetime
    created_by: str
```

In `evidence/store.py`, create `evidence_refs` with unique key:

```sql
UNIQUE(project_id, target_id, service_name, source_kind, scope, source_ref, cursor, content_sha256)
```

`create_from_log_record()` must compute:

```python
content_sha256 = hashlib.sha256(record.message_redacted.encode("utf-8")).hexdigest()
evidence_ref_id = "ev-" + hashlib.sha256(
    f"{record.project_id}|{record.target_id}|{record.service_name}|"
    f"{record.source_kind.value}|{record.scope.value}|{record.source_ref}|"
    f"{record.cursor}|{content_sha256}".encode("utf-8")
).hexdigest()[:24]
```

Do not implement update/delete methods.

Update `LogService.__init__` to accept `evidence: EvidenceStore | None = None`. In `query()`, if `create_evidence=True` require `persist=True` and `incident_id is not None`; create evidence from inserted records after store append and set `evidence_ref_id` on returned record using `model_copy(update=...)`.

- [ ] **Step 5: Run tests and lint**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/evidence/test_store.py tests/logs/test_service.py -q
```

Expected: PASS.

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/evidence apps/control-plane/src/incidentlens_control_plane/logs tests/evidence tests/logs
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/evidence apps/control-plane/src/incidentlens_control_plane/logs tests/evidence tests/logs/test_service.py
git commit -m "feat: add append-only redacted evidence store" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: 接入 Runtime 构造和 HTTP Query/Search/Evidence API

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/routes/logs.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/routes/evidence.py`
- Create: `tests/web/test_logs_api.py`
- Create: `tests/web/test_evidence_api.py`
- Modify: `tests/web/conftest.py`

**Interfaces:**
- Consumes: `LogService.query()`, `LogStore.search()`, `EvidenceStore`.
- Produces:
  - `RuntimeServices.logs: LogService`
  - `RuntimeServices.log_store: LogStore`
  - `RuntimeServices.evidence: EvidenceStore`
  - `POST /api/logs/query`
  - `GET /api/logs/search`
  - `POST /api/evidence/from-log-records`
  - `GET /api/evidence/{evidence_ref_id}`
  - `GET /api/incidents/{incident_id}/evidence`

- [ ] **Step 1: Write failing logs API tests**

Create `tests/web/test_logs_api.py`:

```python
def test_logs_query_rejects_unexpected_connection_fields(client, registered_project) -> None:
    response = client.post(
        "/api/logs/query",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service_name": "payment-api",
            "source_kind": "docker",
            "scope": "container",
            "source_ref": "payments-api-1",
            "host": "attacker.example.test",
            "ssh_user": "root",
        },
    )

    assert response.status_code == 422


def test_logs_query_rejects_unregistered_container(client, registered_project) -> None:
    response = client.post(
        "/api/logs/query",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service_name": "payment-api",
            "source_kind": "docker",
            "scope": "container",
            "source_ref": "not-registered",
            "tail_lines": 50,
            "persist": False,
            "create_evidence": False,
        },
    )

    assert response.status_code == 409


def test_logs_search_returns_persisted_redacted_records(client, runtime, registered_project) -> None:
    now = datetime(2026, 8, 12, tzinfo=UTC)
    runtime.log_store.append_batch((
        make_web_log_record("ERROR token=[REDACTED_TOKEN]", now=now),
    ))

    response = client.get(
        "/api/logs/search",
        params={"project_id": "payments", "text": "ERROR", "limit": 10},
    )

    assert response.status_code == 200
    assert response.json()[0]["message_redacted"] == "ERROR token=[REDACTED_TOKEN]"
    assert "abc123" not in response.text
```

- [ ] **Step 2: Write failing evidence API tests**

Create `tests/web/test_evidence_api.py`:

```python
def test_create_evidence_from_log_records(client, runtime, registered_project) -> None:
    record = make_web_log_record("ERROR token=[REDACTED_TOKEN]", now=datetime(2026, 8, 12, tzinfo=UTC))
    runtime.log_store.append_batch((record,))

    response = client.post(
        "/api/evidence/from-log-records",
        json={
            "incident_id": "inc-1",
            "log_ids": ["log-web-1"],
            "created_by": "alice",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body[0]["incident_id"] == "inc-1"
    assert "abc123" not in response.text


def test_get_incident_evidence_lists_redacted_refs(client, runtime, registered_project) -> None:
    record = make_web_log_record("WARN password=[REDACTED_PASSWORD]", now=datetime(2026, 8, 12, tzinfo=UTC))
    evidence = runtime.evidence.create_from_log_record(
        record,
        incident_id="inc-1",
        created_by="alice",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    response = client.get("/api/incidents/inc-1/evidence?limit=10")

    assert response.status_code == 200
    assert response.json()[0]["evidence_ref_id"] == evidence.evidence_ref_id
    assert "hunter2" not in response.text
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/web/test_logs_api.py tests/web/test_evidence_api.py -q
```

Expected: FAIL because routes and runtime fields do not exist.

- [ ] **Step 4: Implement runtime services**

Modify `RuntimeServices`:

```python
log_store: LogStore
evidence: EvidenceStore
logs: LogService
```

In `build_runtime()`:
1. Construct `log_store = LogStore(connect)` and `evidence = EvidenceStore(connect)`.
2. Call `log_store.migrate()` and `evidence.migrate()` with existing migrations.
3. Construct `logs = LogService(projects=projects, store=log_store, sessions=sessions, evidence=evidence)`.

- [ ] **Step 5: Implement route models and error mapping**

In `routes/logs.py`, define request model mirroring `LogQueryRequest` with `extra="forbid"`. Map:
- unknown project/target/service -> 404
- unregistered container -> 409
- unsupported capability/input syntax -> 422
- safe source unavailable -> 502
- timeout -> 504

Minimal handler:

```python
@router.post("/query")
async def query_logs(request: Request, body: LogQueryRequestModel) -> list[dict[str, object]]:
    runtime = get_runtime(request)
    try:
        records = await runtime.logs.query(
            LogQueryRequest(**body.model_dump()),
            now=datetime.now(UTC),
        )
    except UnregisteredLogContainer:
        raise HTTPException(status_code=409, detail="Container is not registered for the service")
    except LogSourceUnavailable:
        raise HTTPException(status_code=502, detail="Log source unavailable")
    return [record.model_dump(mode="json") for record in records]
```

In `routes/evidence.py`, `POST /api/evidence/from-log-records` loads each stored `LogRecord` by id via `LogStore.get_record(log_id)`, then calls `EvidenceStore.create_from_log_record()`.

Include routers in `main.py`:

```python
from incidentlens_control_plane.routes.logs import router as logs_router
from incidentlens_control_plane.routes.evidence import router as evidence_router

application.include_router(logs_router)
application.include_router(evidence_router)
```

- [ ] **Step 6: Run tests and lint**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/web/test_logs_api.py tests/web/test_evidence_api.py tests/test_app.py -q
```

Expected: PASS.

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/runtime.py apps/control-plane/src/incidentlens_control_plane/main.py apps/control-plane/src/incidentlens_control_plane/routes tests/web
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/runtime.py apps/control-plane/src/incidentlens_control_plane/main.py apps/control-plane/src/incidentlens_control_plane/routes/logs.py apps/control-plane/src/incidentlens_control_plane/routes/evidence.py tests/web/test_logs_api.py tests/web/test_evidence_api.py tests/web/conftest.py
git commit -m "feat: expose redacted log and evidence APIs" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: 实现 LogSubscriptionManager 基础状态机、恢复和 file stream

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/config.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/logs/subscriptions.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/logs/sources.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/logs/store.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Create: `tests/logs/test_subscriptions.py`

**Interfaces:**
- Consumes: `LogStore`, `LogService`, `FileLogSource.stream()`, `RuntimeEventStore`, `RuntimeEventBroker`.
- Produces:
  - `RuntimeSettings.max_active_log_subscriptions: int = 20`
  - `RuntimeSettings.log_subscription_queue_size: int = 1000`
  - `RuntimeSettings.log_subscription_batch_size: int = 100`
  - `RuntimeSettings.log_file_poll_interval_seconds: float = 2.0`
  - `LogSubscriptionManager.create(...) -> LogSubscription`
  - `LogSubscriptionManager.start_active_opt_in() -> None`
  - `LogSubscriptionManager.pause(subscription_id: str) -> LogSubscription`
  - `LogSubscriptionManager.resume(subscription_id: str) -> LogSubscription`
  - `LogSubscriptionManager.delete(subscription_id: str) -> LogSubscription`
  - `LogSubscriptionManager.close_all() -> None`

- [ ] **Step 1: Write failing opt-in and active limit tests**

Create `tests/logs/test_subscriptions.py`:

```python
@pytest.mark.asyncio
async def test_create_subscription_requires_explicit_opt_in(manager: LogSubscriptionManager) -> None:
    with pytest.raises(ValueError, match="opt_in_streaming=true"):
        await manager.create(
            project_id="payments",
            target_id="dev-a",
            service_name="payment-api",
            source_kind=LogSourceKind.FILE,
            scope=LogScope.HOST,
            source_ref="/var/log/payment/app.log",
            opt_in_streaming=False,
            created_by="alice",
        )


@pytest.mark.asyncio
async def test_active_subscription_limit_returns_domain_error(manager: LogSubscriptionManager) -> None:
    manager.max_active = 1
    await manager.create(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
    )

    with pytest.raises(TooManyActiveSubscriptions):
        await manager.create(
            project_id="payments",
            target_id="dev-a",
            service_name="payment-api",
            source_kind=LogSourceKind.FILE,
            scope=LogScope.HOST,
            source_ref="/var/log/payment/other.log",
            opt_in_streaming=True,
            created_by="alice",
        )
```

- [ ] **Step 2: Write failing recovery and cursor tests**

Add:

```python
@pytest.mark.asyncio
async def test_start_active_opt_in_restores_only_active_subscriptions(manager, store) -> None:
    active = store.create_subscription(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    paused = store.create_subscription(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/paused.log",
        opt_in_streaming=True,
        created_by="alice",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    store.pause_subscription(paused.subscription_id, now=datetime(2026, 8, 12, tzinfo=UTC))

    await manager.start_active_opt_in()

    assert active.subscription_id in manager.running_subscription_ids()
    assert paused.subscription_id not in manager.running_subscription_ids()


@pytest.mark.asyncio
async def test_pause_stops_task_and_preserves_cursor(manager, store) -> None:
    subscription = await manager.create(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
    )
    store.upsert_cursor(
        subscription_id=subscription.subscription_id,
        cursor="file:offset=42",
        generation="mtime=1:size=42",
        observed_at=datetime(2026, 8, 12, tzinfo=UTC),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    paused = await manager.pause(subscription.subscription_id)

    assert paused.status.value == "paused"
    assert store.get_cursor(subscription.subscription_id).cursor == "file:offset=42"
    assert subscription.subscription_id not in manager.running_subscription_ids()
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/logs/test_subscriptions.py -q
```

Expected: FAIL because `logs.subscriptions` does not exist and runtime settings are missing.

- [ ] **Step 4: Implement manager and file stream**

Add settings to `RuntimeSettings`:

```python
max_active_log_subscriptions: int = 20
log_subscription_queue_size: int = 1000
log_subscription_batch_size: int = 100
log_file_poll_interval_seconds: float = 2.0
```

Implement `FileLogSource.stream(subscription, cursor)` using SFTP `lstat/read_bytes` polling:
- Cursor format: `file:offset=<int>`.
- Generation format: `mtime=<modified_ns>:size=<size>`.
- If `size < offset`, emit/return rotate signal through manager and restart offset at 0.
- Queue raw lines with cursor per line after newline splitting.
- Do not run `tail -f`.

Implement `LogSubscriptionManager`:
- `create()` persists subscription as active only when `opt_in_streaming=True`.
- `start_active_opt_in()` starts all active opt-in subscriptions from store.
- Each running subscription owns one reader task, one writer task, one bounded queue.
- Writer calls `LogService.process_raw_lines(...)` or internal equivalent, then `LogStore.append_batch()`, then `LogStore.upsert_cursor()` after transaction success.
- `pause()` cancels/awaits tasks then updates status.
- `close_all()` awaits all tasks and does not close SSH sessions.

Modify `main._lifespan()` finally order:

```python
await services.subscriptions.close_all()
await services.sessions.close_all()
```

- [ ] **Step 5: Run tests and lint**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/logs/test_subscriptions.py tests/logs/test_store.py tests/test_app.py -q
```

Expected: PASS.

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/config.py apps/control-plane/src/incidentlens_control_plane/logs apps/control-plane/src/incidentlens_control_plane/runtime.py apps/control-plane/src/incidentlens_control_plane/main.py tests/logs
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/config.py apps/control-plane/src/incidentlens_control_plane/logs apps/control-plane/src/incidentlens_control_plane/runtime.py apps/control-plane/src/incidentlens_control_plane/main.py tests/logs/test_subscriptions.py
git commit -m "feat: manage persistent log subscriptions" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: 实现 Docker streaming、背压、重试和安全 runtime events

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/events/types.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/logs/sources.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/logs/subscriptions.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/fakes.py`
- Modify: `tests/logs/test_sources_docker.py`
- Modify: `tests/logs/test_subscriptions.py`
- Modify: `tests/events/test_store.py`

**Interfaces:**
- Consumes: `RemoteTransport.open_process(argv, term_type=None)`, `RuntimeEventStore.append()`, `RuntimeEventBroker.publish()`.
- Produces new `RuntimeEventType` values:
  - `LOG_SUBSCRIPTION_STARTED = "log.subscription_started"`
  - `LOG_SUBSCRIPTION_PAUSED = "log.subscription_paused"`
  - `LOG_SUBSCRIPTION_RESUMED = "log.subscription_resumed"`
  - `LOG_SUBSCRIPTION_DELETED = "log.subscription_deleted"`
  - `LOG_BATCH_WRITTEN = "log.batch_written"`
  - `LOG_SOURCE_ROTATED = "log.source_rotated"`
  - `LOG_BACKPRESSURE = "log.backpressure"`
  - `LOG_SUBSCRIPTION_ERROR = "log.subscription_error"`

- [ ] **Step 1: Write failing Docker stream argv and dedupe boundary tests**

Add to `tests/logs/test_sources_docker.py`:

```python
@pytest.mark.asyncio
async def test_docker_stream_uses_since_cursor_and_follow_argv(target_registration) -> None:
    transport = FakeChangeTransport()
    transport.process_chunks = [
        b"2026-08-12T10:00:00Z INFO one\n",
        b"2026-08-12T10:00:01Z INFO two\n",
    ]
    source = DockerLogSource(lambda target: transport)

    lines = []
    async for line in source.stream(
        subscription=docker_subscription("payments-api-1"),
        target=target_registration,
        cursor="docker:time=2026-08-12T09:59:59Z:seq=0",
    ):
        lines.append(line)
        if len(lines) == 2:
            break

    assert [line.cursor for line in lines] == [
        "docker:time=2026-08-12T10:00:00Z:seq=1",
        "docker:time=2026-08-12T10:00:01Z:seq=2",
    ]
    assert transport.open_process_calls == [
        (
            (
                "docker",
                "logs",
                "--timestamps",
                "--follow",
                "--since",
                "2026-08-12T09:59:59Z",
                "--",
                "payments-api-1",
            ),
            None,
        )
    ]
```

- [ ] **Step 2: Write failing backpressure and redacted event tests**

Add to `tests/logs/test_subscriptions.py`:

```python
@pytest.mark.asyncio
async def test_docker_backpressure_closes_process_and_emits_safe_event(manager, runtime_events) -> None:
    subscription = await manager.create(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.DOCKER,
        scope=LogScope.CONTAINER,
        source_ref="payments-api-1",
        opt_in_streaming=True,
        created_by="alice",
    )
    await manager.force_backpressure_for_test(subscription.subscription_id)

    events = runtime_events.list_after(0, limit=100)
    payloads = [event.payload for event in events if event.event_type.value == "log.backpressure"]

    assert payloads
    assert "token=abc123" not in json.dumps(payloads)
    assert "dev-a.example.test" not in json.dumps(payloads)


@pytest.mark.asyncio
async def test_repeated_errors_move_subscription_to_error_with_redacted_summary(manager, store) -> None:
    manager.max_failures = 2
    await manager.record_failure_for_test("sub-1", RuntimeError("token=abc123 host dev-a.example.test"))
    await manager.record_failure_for_test("sub-1", RuntimeError("token=abc123 host dev-a.example.test"))

    subscription = store.get_subscription("sub-1")
    assert subscription.status.value == "error"
    assert "abc123" not in subscription.last_error_redacted
    assert "dev-a.example.test" not in subscription.last_error_redacted
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/logs/test_sources_docker.py tests/logs/test_subscriptions.py tests/events/test_store.py -q
```

Expected: FAIL because Docker stream, backpressure hooks, and log event types are missing.

- [ ] **Step 4: Implement Docker stream and safe events**

In `DockerLogSource.stream()`:
- Parse cursor time from `docker:time=<iso>:seq=<n>`, default to current `observed_at - 1 second` or configured bootstrap.
- Open fixed process:

```python
process = await transport.open_process(
    (
        "docker",
        "logs",
        "--timestamps",
        "--follow",
        "--since",
        since_time,
        "--",
        subscription.source_ref,
    ),
    term_type=None,
)
```

- Read chunks, split lines, parse Docker timestamp prefix, increment batch sequence for same timestamp.
- Never persist stderr as application log; convert process failure to `LogSourceUnavailable("docker log stream unavailable")`.

In `LogSubscriptionManager`:
- `_emit_safe_event(event_type, subscription, payload)` only includes subscription id, project, target id, service, source kind, source ref, status, counts, redacted summary.
- Backpressure: if queue `put()` exceeds configured timeout for Docker, close process, emit `log.backpressure`, reconnect from last committed cursor.
- Retry: exponential backoff capped at 60 seconds; after threshold, set status `error`.

- [ ] **Step 5: Run tests and lint**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/logs/test_sources_docker.py tests/logs/test_subscriptions.py tests/events -q
```

Expected: PASS.

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/events apps/control-plane/src/incidentlens_control_plane/logs apps/control-plane/src/incidentlens_control_plane/remote_ops/fakes.py tests/logs tests/events
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/events/types.py apps/control-plane/src/incidentlens_control_plane/logs apps/control-plane/src/incidentlens_control_plane/remote_ops/fakes.py tests/logs tests/events
git commit -m "feat: stream docker logs with safe backpressure" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: 实现订阅 HTTP API 和 WebSocket replay/live 去重

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/routes/logs.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/logs/store.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/logs/subscriptions.py`
- Create: `tests/web/test_log_subscriptions_api.py`

**Interfaces:**
- Consumes: `LogSubscriptionManager`, `LogStore.list_records_for_subscription(subscription_id, after_cursor, limit)`.
- Produces:
  - `POST /api/logs/subscriptions`
  - `GET /api/logs/subscriptions`
  - `GET /api/logs/subscriptions/{subscription_id}`
  - `POST /api/logs/subscriptions/{subscription_id}/pause`
  - `POST /api/logs/subscriptions/{subscription_id}/resume`
  - `DELETE /api/logs/subscriptions/{subscription_id}`
  - `GET /api/logs/subscriptions/{subscription_id}/records`
  - `WS /api/logs/subscriptions/{subscription_id}/ws`

- [ ] **Step 1: Write failing subscription API tests**

Create `tests/web/test_log_subscriptions_api.py`:

```python
def test_create_subscription_requires_opt_in_true(client, registered_project) -> None:
    response = client.post(
        "/api/logs/subscriptions",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service_name": "payment-api",
            "source_kind": "docker",
            "scope": "container",
            "source_ref": "payments-api-1",
            "opt_in_streaming": False,
            "created_by": "alice",
        },
    )

    assert response.status_code == 400
    assert "opt_in_streaming=true" in response.text


def test_pause_resume_delete_subscription_state_machine(client, registered_project) -> None:
    created = client.post(
        "/api/logs/subscriptions",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service_name": "payment-api",
            "source_kind": "docker",
            "scope": "container",
            "source_ref": "payments-api-1",
            "opt_in_streaming": True,
            "created_by": "alice",
        },
    )
    subscription_id = created.json()["subscription_id"]

    paused = client.post(f"/api/logs/subscriptions/{subscription_id}/pause")
    resumed = client.post(f"/api/logs/subscriptions/{subscription_id}/resume")
    deleted = client.delete(f"/api/logs/subscriptions/{subscription_id}")

    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"
    assert deleted.status_code == 204
```

- [ ] **Step 2: Write failing WebSocket replay/live dedupe test**

Add:

```python
def test_subscription_websocket_replays_then_streams_without_duplicate(
    client, runtime, registered_project
) -> None:
    subscription = runtime.log_store.create_subscription(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    record = make_subscription_record(subscription.subscription_id, "cursor-1")
    runtime.log_store.append_batch((record,))

    with client.websocket_connect(
        f"/api/logs/subscriptions/{subscription.subscription_id}/ws"
    ) as socket:
        replayed = socket.receive_json()
        runtime.subscriptions.publish_live_for_test(record)
        live = socket.receive_json()

    assert replayed["log_id"] == record.log_id
    assert live["event"] == "heartbeat"
```

The second message is a heartbeat because duplicate live record with the same `dedupe_key` is skipped.

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/web/test_log_subscriptions_api.py -q
```

Expected: FAIL because endpoints and subscription WebSocket do not exist.

- [ ] **Step 4: Implement route handlers**

In `routes/logs.py`, add models with `extra="forbid"`:
- `CreateLogSubscriptionRequest`
- `LogSubscriptionView`
- `LogRecordView`

Error mapping:
- opt-in false -> 400
- unknown subscription -> 404
- invalid pause/resume/delete transition -> 409
- active over limit -> 429

WebSocket strategy:
1. `await websocket.accept()`.
2. Register live queue before replay: `async with runtime.subscriptions.subscribe_records(subscription_id) as queue:`.
3. Replay durable records from store in cursor/log_id order.
4. Track sent `dedupe_key` set bounded to replay count + live session count.
5. Stream live records; skip if dedupe seen.
6. On disconnect, exit only the socket loop; do not pause/stop subscription.

Heartbeat implementation sends `{"event": "heartbeat"}` when test hook emits duplicate and no unique event is available.

- [ ] **Step 5: Run tests and lint**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/web/test_log_subscriptions_api.py tests/web/test_logs_api.py tests/web/test_events_api.py -q
```

Expected: PASS.

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/routes/logs.py apps/control-plane/src/incidentlens_control_plane/logs tests/web/test_log_subscriptions_api.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/routes/logs.py apps/control-plane/src/incidentlens_control_plane/logs tests/web/test_log_subscriptions_api.py
git commit -m "feat: expose persistent log subscriptions" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: 完成 runtime lifecycle、集成回归和 opt-in live verification 文档

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Create: `tests/integration/test_live_log_tools.py`
- Create: `docs/phase-3-hybrid-log-evidence-verification.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: all Phase 3 public APIs.
- Produces: default-off live acceptance test controlled by `INCIDENTLENS_RUN_LIVE_LOG_TESTS=1`.

- [ ] **Step 1: Write lifecycle regression test**

Add to `tests/web/test_log_subscriptions_api.py`:

```python
def test_lifespan_restores_subscriptions_before_requests_and_closes_before_sessions(tmp_path) -> None:
    calls: list[str] = []

    class TrackingSubscriptions:
        async def start_active_opt_in(self) -> None:
            calls.append("subscriptions.start")

        async def close_all(self) -> None:
            calls.append("subscriptions.close")

    class TrackingSessions:
        async def close_all(self) -> None:
            calls.append("sessions.close")

    services = build_runtime(RuntimeSettings(data_dir=tmp_path / "data"))
    services = services.model_copy(
        update={
            "subscriptions": TrackingSubscriptions(),
            "sessions": TrackingSessions(),
        }
    )

    assert calls == ["subscriptions.start", "subscriptions.close", "sessions.close"]
```

If `RuntimeServices` remains a frozen dataclass and cannot `model_copy`, implement the same assertion with a patched `build_runtime()` in `incidentlens_control_plane.main`.

- [ ] **Step 2: Write default-skipped live test skeleton**

Create `tests/integration/test_live_log_tools.py`:

```python
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("INCIDENTLENS_RUN_LIVE_LOG_TESTS") != "1",
    reason="set INCIDENTLENS_RUN_LIVE_LOG_TESTS=1 to run opt-in SSH/Docker log tests",
)


def test_live_container_list_search_file_docker_stream_restart_dedupe_and_evidence() -> None:
    assert os.environ["INCIDENTLENS_RUN_LIVE_LOG_TESTS"] == "1"
```

Then replace the placeholder body in the same task with real calls against the test SSH/Docker environment:
1. Register project with host/container/log allowlists.
2. Verify container list/search.
3. Query host file log and assert sensitive token is redacted.
4. Query Docker logs with fixed registered container.
5. Create opt-in subscription and append a new log line.
6. Restart app with same `data_dir`.
7. Verify cursor resume and no duplicate record for replay boundary.
8. Create evidence and assert only redacted content.

Use existing integration style from `tests/integration/test_live_ssh_tools.py`: skip unless explicit env and infrastructure are available.

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/web/test_log_subscriptions_api.py::test_lifespan_restores_subscriptions_before_requests_and_closes_before_sessions tests/integration/test_live_log_tools.py -q
```

Expected: initial lifecycle test FAIL until startup/shutdown order is implemented; live test SKIPPED unless `INCIDENTLENS_RUN_LIVE_LOG_TESTS=1`.

- [ ] **Step 4: Implement lifecycle order**

In `main._lifespan()`:

```python
services = build_runtime(settings, transport_factory=transport_factory)
app.state.runtime = services
await services.subscriptions.start_active_opt_in()
try:
    yield
finally:
    await services.subscriptions.close_all()
    await services.sessions.close_all()
    app.state.runtime = None
```

If one subscription restore fails, catch inside `LogSubscriptionManager.start_active_opt_in()` and mark that subscription retry/error; do not fail app startup. Do not catch migration failures in `build_runtime()`.

- [ ] **Step 5: Document verification**

Create `docs/phase-3-hybrid-log-evidence-verification.md` with exact commands:

```markdown
# Phase 3 Hybrid Log Evidence Verification

## Default offline checks

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/logs tests/evidence tests/remote_ops tests/web tests/events tests/test_app.py -q
UV_CACHE_DIR=.uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane tests
```

## Opt-in live checks

```bash
INCIDENTLENS_RUN_LIVE_LOG_TESTS=1 UV_CACHE_DIR=.uv-cache uv run pytest tests/integration/test_live_log_tools.py -q
```

Live checks require the existing test SSH/Docker environment and never run by default.
```

Update `README.md` to list Phase 3 APIs and repeat the default-off live command.

- [ ] **Step 6: Run final offline suite and lint**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/logs tests/evidence tests/remote_ops tests/web tests/events tests/test_app.py -q
```

Expected: PASS.

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/integration/test_live_log_tools.py -q
```

Expected: SKIPPED when `INCIDENTLENS_RUN_LIVE_LOG_TESTS` is unset.

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane tests
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/main.py apps/control-plane/src/incidentlens_control_plane/runtime.py tests/integration/test_live_log_tools.py docs/phase-3-hybrid-log-evidence-verification.md README.md tests/web/test_log_subscriptions_api.py
git commit -m "test: verify phase 3 log evidence lifecycle" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review Checklist

- Spec coverage:
  - Phase 2 container list/search: Task 1.
  - 日志领域模型、severity、timestamp、normal signal、correlation：Task 2.
  - 脱敏、16 KiB 截断、禁止 raw 持久化：Task 2, Task 3, Task 5, Task 6.
  - SQLite log_records、FTS5、subscriptions、cursors、runs：Task 3.
  - 按需 file/docker query、fixed Docker argv、registered container/path：Task 4, Task 6.
  - Evidence Store append-only、hash redacted content、idempotency：Task 5, Task 6.
  - opt-in persistent subscription、恢复、pause/resume/delete、cursor：Task 7, Task 9, Task 10.
  - Docker streaming、backpressure、retry、safe runtime events：Task 8.
  - WebSocket replay/live dedupe：Task 9.
  - Runtime lifecycle shutdown order：Task 7, Task 10.
  - Live verification/default offline tests/docs：Task 10.
- Placeholder scan:
  - 已逐项扫描禁止占位模式，未发现未解决的占位内容。
  - 每个任务都有实际测试代码、实现片段、pytest/ruff 命令、预期结果和 commit 命令。
- Type consistency:
  - `service_name` 在 logs/evidence/API 中一致；现有 project registry 的字段仍为 `compose_service`，解析时映射为 service name。
  - `source_kind`、`scope` 均使用 enum 值 `"file"|"docker"` 和 `"host"|"container"`。
  - `message_redacted` 是唯一持久化/返回内容字段；没有 `raw_message` 或 `content_raw`。

### Critical Files for Implementation

- `apps/control-plane/src/incidentlens_control_plane/logs/types.py`
- `apps/control-plane/src/incidentlens_control_plane/logs/store.py`
- `apps/control-plane/src/incidentlens_control_plane/logs/service.py`
- `apps/control-plane/src/incidentlens_control_plane/logs/subscriptions.py`
- `apps/control-plane/src/incidentlens_control_plane/runtime.py`