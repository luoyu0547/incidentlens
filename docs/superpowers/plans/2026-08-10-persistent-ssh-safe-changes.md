# Persistent SSH Tools and Safe Changes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add reusable SSH host sessions and host/container Read, List, Search, Stat, Edit, Write, Restore, and Shell tools with mandatory dual backups, durable approvals, atomic changes, verification, and rollback.

**Architecture:** `SessionManager` owns one AsyncSSH connection per registered target and opens SFTP, command, and persistent PTY channels on that connection. `RemoteToolGateway` accepts typed operations, resolves project policy before transport access, and delegates all writes to `ChangeManager`; the model never receives SSH credentials or an unrestricted transport object. SQLite stores approvals and ChangeSets, while encrypted local backups and timestamped same-directory remote backups make every overwrite recoverable.

**Tech Stack:** Python 3.12, FastAPI 0.115+, Pydantic 2.13+, AsyncSSH 2.24+, cryptography AES-GCM, stdlib `asyncio`/`sqlite3`/`hashlib`/`shlex`, pytest 8, Ruff.

## Global Constraints

- IncidentLens runs only on the developer's local computer; no IncidentLens process is installed on a server.
- Docker Compose over SSH is the only MVP runtime.
- Reuse a persistent SSH connection instead of opening one connection per tool call.
- The model may call typed remote tools but never sees SSH private-key bytes or a raw AsyncSSH connection.
- Ordinary remote edits are automatic only after both encrypted local backup and timestamped same-directory remote backup succeed and match the original SHA-256.
- `rm -rf`, `rm -fr`, split recursive/force flags, and obvious equivalent recursive-force `rm` forms are permanently rejected regardless of approval.
- Docker stop, restart, kill, rm, Compose stop/restart/down/up, image changes, network changes, package installation, and any service interruption require one exact, single-use approval.
- Approval is bound to canonical operation parameters and content hash; any change invalidates it.
- Remote writes use same-directory temporary files and atomic replacement; a stale source hash stops the write.
- Local source code may be edited, but Git commit/push, image publishing, and production deployment remain out of scope.
- Keep Python at `>=3.12,<3.13`; do not add an ORM, task queue, or server-side daemon.
- Phase 1's 25 tests and Ruff check must continue to pass.

Reference: [AsyncSSH official API documentation](https://asyncssh.readthedocs.io/en/latest/api.html) documents one client connection opening multiple sessions, `create_process()`, `run()`, default user SSH keys/known_hosts, and SFTP support.

---

## File Structure

```text
apps/control-plane/src/incidentlens_control_plane/
  approvals/
    __init__.py               approval exports
    service.py                canonical hashing and single-use decisions
    store.py                  SQLite approval repository
    types.py                  immutable approval models
  changes/
    __init__.py               ChangeSet exports
    backup.py                 encrypted local backup vault
    manager.py                preflight, backup, apply, verify, rollback
    store.py                  SQLite ChangeSet journal
    types.py                  ChangeSet and file-change states
  project_registry/
    types.py                  remote path allowlists and SSH target options
  remote_ops/
    asyncssh_adapter.py       AsyncSSH implementation only
    files.py                  scoped Read/List/Search/Stat operations
    gateway.py                policy-first public tool facade
    policy.py                 file and shell risk decisions
    sessions.py               host/container session lifecycle
    shell.py                  persistent PTY framing and output limits
    transport.py              provider-neutral transport protocols
    types.py                  typed scopes, requests, results, decisions
  routes/
    approvals.py              approve/reject endpoints
    changes.py                ChangeSet inspection/rollback endpoints
    remote_sessions.py        connect/disconnect/status endpoints
  runtime.py                  add session, approval, backup, and change services
  events/types.py             add remote/approval/change event kinds
infra/test-ssh/
  Dockerfile                  disposable OpenSSH test target
  compose.yaml                isolated integration target
tests/
  approvals/
  changes/
  remote_ops/
    conftest.py               shared registered target/service/request fixtures
    fakes.py                  deterministic fake transport
    test_asyncssh_adapter.py
    test_files.py
    test_gateway.py
    test_policy.py
    test_sessions.py
    test_shell.py
  integration/test_live_ssh_tools.py
  web/test_approvals_api.py
  web/test_remote_sessions_api.py
```

## Task 1: Extend Registration and Remote Operation Contracts

**Files:**
- Modify: `pyproject.toml`
- Modify: `apps/control-plane/src/incidentlens_control_plane/project_registry/types.py`
- Replace: `apps/control-plane/src/incidentlens_control_plane/remote_ops/types.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/__init__.py`
- Modify: `tests/project_registry/test_types.py`
- Modify: `tests/remote_ops/test_policy.py`
- Modify: `uv.lock` through `uv lock`

**Interfaces:**
- Consumes: Phase 1 `TargetRegistration`, `ServiceRegistration`, and `ProjectRecord`.
- Produces: `RemoteScope`, `HostScope`, `ContainerScope`, `FileOperationKind`, `FileOperationRequest`, `FileEditRequest`, `FileWriteRequest`, `ChangeSetRequest`, `TextReplacement`, `ShellRequest`, `DockerActionKind`, `DockerActionRequest`, `OperationRisk`, and expanded registration path policy.

- [ ] **Step 1: Write failing contract tests**

```python
from pathlib import PurePosixPath

import pytest
from incidentlens_control_plane.project_registry.types import ServiceRegistration
from incidentlens_control_plane.remote_ops.types import (
    ContainerScope,
    FileOperationKind,
    FileOperationRequest,
    HostScope,
    OperationRisk,
    ShellRequest,
)
from pydantic import ValidationError


def test_service_registration_accepts_absolute_remote_roots() -> None:
    service = ServiceRegistration(
        compose_service="payment-api",
        allowed_host_paths=(PurePosixPath("/opt/payments"),),
        allowed_container_paths=(PurePosixPath("/app"),),
        protected_remote_paths=(PurePosixPath("/opt/payments/.env"),),
    )
    assert service.allowed_container_paths == (PurePosixPath("/app"),)


def test_service_registration_rejects_relative_remote_root() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        ServiceRegistration(
            compose_service="payment-api",
            allowed_host_paths=(PurePosixPath("opt/payments"),),
        )


def test_typed_requests_never_contain_credentials() -> None:
    request = FileOperationRequest(
        operation_id="op-1",
        incident_id="inc-1",
        project_id="payments",
        target_id="dev-a",
        service="payment-api",
        scope=ContainerScope(container="payments-api-1"),
        kind=FileOperationKind.READ,
        path=PurePosixPath("/app/service.py"),
    )
    assert "credential" not in request.model_dump()
    assert request.scope.kind == "container"


def test_shell_request_requires_nonempty_reason() -> None:
    with pytest.raises(ValidationError):
        ShellRequest(
            operation_id="op-2",
            incident_id="inc-1",
            project_id="payments",
            target_id="dev-a",
            service="payment-api",
            scope=HostScope(),
            command="pwd",
            reason="",
        )


def test_operation_risks_are_ordered_by_explicit_policy_not_enum_value() -> None:
    assert set(OperationRisk) == {
        OperationRisk.AUTO_READ,
        OperationRisk.BACKUP_REQUIRED,
        OperationRisk.APPROVAL_REQUIRED,
        OperationRisk.FORBIDDEN,
    }
```

- [ ] **Step 2: Run focused tests and confirm contract failures**

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run pytest tests/project_registry/test_types.py tests/remote_ops/test_policy.py -q`

Expected: FAIL because remote path fields and the new request types do not exist.

- [ ] **Step 3: Implement exact domain shapes**

Add to `ServiceRegistration`:

```python
allowed_host_paths: tuple[PurePosixPath, ...] = ()
allowed_container_paths: tuple[PurePosixPath, ...] = ()
protected_remote_paths: tuple[PurePosixPath, ...] = ()
```

Add these optional fields to `TargetRegistration`:

```python
port: int | None = Field(default=None, ge=1, le=65535)
compose_working_directory: PurePosixPath | None = None
compose_project_name: str | None = Field(default=None, min_length=1, max_length=120)
```

Validate every remote path with `path.is_absolute()` and reject any path whose `.parts` contain `".."`. Validate `compose_working_directory` the same way. Defaults preserve existing Phase 1 JSON records.

Replace the old generic apply-change contract with these immutable public types:

```python
class ScopeKind(StrEnum):
    HOST = "host"
    CONTAINER = "container"


class HostScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal[ScopeKind.HOST] = ScopeKind.HOST


class ContainerScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal[ScopeKind.CONTAINER] = ScopeKind.CONTAINER
    container: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


RemoteScope = Annotated[HostScope | ContainerScope, Field(discriminator="kind")]


class FileOperationKind(StrEnum):
    READ = "read"
    LIST = "list"
    SEARCH = "search"
    STAT = "stat"
    EDIT = "edit"
    WRITE = "write"
    RESTORE = "restore"


class OperationRisk(StrEnum):
    AUTO_READ = "auto_read"
    BACKUP_REQUIRED = "backup_required"
    APPROVAL_REQUIRED = "approval_required"
    FORBIDDEN = "forbidden"


class OperationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: str = Field(min_length=1, max_length=120)
    incident_id: str = Field(min_length=1, max_length=120)
    project_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)
    service: str = Field(min_length=1, max_length=120)
    scope: RemoteScope
    session_id: str | None = Field(default=None, min_length=1, max_length=120)


class FileOperationRequest(OperationContext):
    kind: FileOperationKind
    path: PurePosixPath


class TextReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    old_text: str = Field(min_length=1, max_length=1_000_000)
    new_text: str = Field(max_length=1_000_000)
    expected_count: int = Field(default=1, ge=1, le=1_000)


class FileEditRequest(OperationContext):
    kind: Literal[FileOperationKind.EDIT] = FileOperationKind.EDIT
    path: PurePosixPath
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replacements: tuple[TextReplacement, ...] = Field(min_length=1, max_length=200)


class FileWriteRequest(OperationContext):
    kind: Literal[FileOperationKind.WRITE] = FileOperationKind.WRITE
    path: PurePosixPath
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content: bytes = Field(max_length=10_485_760)
    mode: int | None = Field(default=None, ge=0, le=0o7777)


FileMutationRequest = Annotated[
    FileEditRequest | FileWriteRequest,
    Field(discriminator="kind"),
]


class ChangeSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    changeset_id: str = Field(min_length=1, max_length=120)
    files: tuple[FileMutationRequest, ...] = Field(min_length=1, max_length=100)
    verification_plan: str = Field(min_length=1, max_length=4_000)
    rollback_plan: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def files_share_one_operation_context(self) -> "ChangeSetRequest":
        first = self.files[0]
        context = (
            first.incident_id,
            first.project_id,
            first.target_id,
            first.service,
            first.scope,
        )
        if any(
            (
                item.incident_id,
                item.project_id,
                item.target_id,
                item.service,
                item.scope,
            )
            != context
            for item in self.files[1:]
        ):
            raise ValueError("all files must share one operation context")
        return self


class ShellRequest(OperationContext):
    command: str = Field(min_length=1, max_length=8_000)
    reason: str = Field(min_length=1, max_length=1_000)


class DockerActionKind(StrEnum):
    STOP = "stop"
    RESTART = "restart"
    KILL = "kill"
    REMOVE = "remove"
    COMPOSE_STOP = "compose_stop"
    COMPOSE_RESTART = "compose_restart"
    COMPOSE_DOWN = "compose_down"
    COMPOSE_UP = "compose_up"


class DockerActionRequest(OperationContext):
    action: DockerActionKind
    container: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
    )
    reason: str = Field(min_length=1, max_length=1_000)
```

Add a model validator requiring `container` for STOP/RESTART/KILL/REMOVE and forbidding it for Compose actions. Container actions must later match a registered `container_names` entry. Every Docker action is `APPROVAL_REQUIRED`; none is inferred from image naming.

Keep `RuntimeKind.DOCKER_COMPOSE` only; remove Kubernetes from the MVP contract. Export all new values from `remote_ops/__init__.py`.

Add dependencies `asyncssh>=2.24,<3` and `cryptography>=45,<47`, then run `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv lock`.

- [ ] **Step 4: Replace legacy contract tests and run them**

Remove assertions tied to deleted `TargetProfile`, `RemoteAction`, and `ChangeControls`. Keep the credential-exclusion assertion from Step 1, and add:

```python
def test_edit_request_carries_exact_version_and_multiple_replacements() -> None:
    request = FileEditRequest(
        operation_id="op-3",
        incident_id="inc-1",
        project_id="payments",
        target_id="dev-a",
        service="payment-api",
        scope=HostScope(),
        path=PurePosixPath("/opt/payments/app.py"),
        expected_sha256="a" * 64,
        replacements=(
            TextReplacement(old_text="old_a", new_text="new_a"),
            TextReplacement(old_text="old_b", new_text="new_b"),
        ),
    )
    assert len(request.replacements) == 2


def test_write_request_limits_payload_to_ten_mib() -> None:
    with pytest.raises(ValidationError):
        FileWriteRequest(
            operation_id="op-4",
            incident_id="inc-1",
            project_id="payments",
            target_id="dev-a",
            service="payment-api",
            scope=HostScope(),
            path=PurePosixPath("/opt/payments/generated.py"),
            content=b"x" * (10_485_760 + 1),
        )
```

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run pytest tests/project_registry tests/remote_ops/test_policy.py -q`

Expected: PASS.

- [ ] **Step 5: Lint and commit**

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/project_registry apps/control-plane/src/incidentlens_control_plane/remote_ops tests/project_registry tests/remote_ops`

Expected: PASS.

```bash
git add pyproject.toml uv.lock apps/control-plane/src/incidentlens_control_plane/project_registry apps/control-plane/src/incidentlens_control_plane/remote_ops tests/project_registry tests/remote_ops
git commit -m "refactor: define scoped remote operation contracts"
```

## Task 2: Build the AsyncSSH Transport and Persistent Host Sessions

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/remote_ops/transport.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/remote_ops/asyncssh_adapter.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/remote_ops/sessions.py`
- Create: `tests/remote_ops/fakes.py`
- Create: `tests/remote_ops/conftest.py`
- Create: `tests/remote_ops/test_asyncssh_adapter.py`
- Create: `tests/remote_ops/test_sessions.py`

**Interfaces:**
- Consumes: `TargetRegistration` from Task 1 and AsyncSSH connection/SFTP/process APIs.
- Produces: `CommandResult`, `FileMetadata`, `RemoteTransport`, `RemoteTransportFactory`, `AsyncSshTransportFactory`, `HostSession`, `ContainerSession`, and `SessionManager`.

- [ ] **Step 1: Create shared fixtures and write failing session reuse tests**

Create this fixture in `tests/remote_ops/conftest.py`:

```python
@pytest.fixture
def target_registration() -> TargetRegistration:
    return TargetRegistration(
        target_id="dev-a",
        host="dev-a.example.test",
        ssh_user="deploy",
        ssh_config_alias="dev-a",
    )
```

```python
import asyncio

from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
from incidentlens_control_plane.remote_ops.sessions import SessionManager


def test_session_manager_reuses_one_transport_per_target(target_registration) -> None:
    async def scenario() -> None:
        factory = FakeTransportFactory()
        manager = SessionManager(factory)
        first = await manager.connect(target_registration)
        second = await manager.connect(target_registration)

        assert first is second
        assert factory.connect_calls == [target_registration]
        await manager.close_all()
        assert factory.transports[0].closed is True

    asyncio.run(scenario())


def test_dead_transport_is_reconnected_without_reusing_unknown_shell_state(
    target_registration,
) -> None:
    async def scenario() -> None:
        factory = FakeTransportFactory()
        manager = SessionManager(factory)
        first = await manager.connect(target_registration)
        first.transport.alive = False

        second = await manager.connect(target_registration)

        assert second.session_id != first.session_id
        assert second.host_process is None
        assert len(factory.connect_calls) == 2

    asyncio.run(scenario())


def test_container_session_is_an_independent_child(target_registration) -> None:
    async def scenario() -> None:
        manager = SessionManager(FakeTransportFactory())
        host = await manager.connect(target_registration)
        child = await manager.spawn_container_session(host.session_id, "payments-api-1")

        assert child.session_id != host.session_id
        assert child.parent_session_id == host.session_id
        await manager.close_container_session(child.session_id)
        assert await host.transport.is_alive() is True

    asyncio.run(scenario())
```

- [ ] **Step 2: Write adapter construction tests without network access**

Patch `asyncssh.connect` with `AsyncMock` and assert `AsyncSshTransportFactory.connect()` uses `ssh_config_alias or host`, registered username, optional port, `known_hosts=()` to retain AsyncSSH's normal known-hosts lookup, and keepalive interval/count. Assert the returned adapter opens one SFTP client lazily and closes SFTP plus connection exactly once.

```python
connect.assert_awaited_once_with(
    "dev-a",
    username="deploy",
    known_hosts=(),
    keepalive_interval=15,
    keepalive_count_max=3,
)
```

Do not pass private-key content or disable host-key validation. A future interactive host-key enrollment flow is outside this task; unknown hosts fail closed with the AsyncSSH error.

- [ ] **Step 3: Implement transport protocols and adapter**

Define the provider-neutral boundary:

```python
@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_status: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class FileMetadata:
    path: PurePosixPath
    size: int
    mode: int
    uid: int
    gid: int
    modified_ns: int
    is_symlink: bool


class RemoteProcess(Protocol):
    async def write(self, data: bytes) -> None:
        raise NotImplementedError

    async def read(self, max_bytes: int) -> bytes:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class RemoteTransport(Protocol):
    async def is_alive(self) -> bool:
        raise NotImplementedError

    async def realpath(self, path: PurePosixPath) -> PurePosixPath:
        raise NotImplementedError

    async def lstat(self, path: PurePosixPath) -> FileMetadata:
        raise NotImplementedError

    async def read_bytes(self, path: PurePosixPath, *, max_bytes: int) -> bytes:
        raise NotImplementedError

    async def list_directory(self, path: PurePosixPath) -> tuple[FileMetadata, ...]:
        raise NotImplementedError

    async def write_bytes(self, path: PurePosixPath, content: bytes, *, mode: int) -> None:
        raise NotImplementedError

    async def rename(self, source: PurePosixPath, target: PurePosixPath) -> None:
        raise NotImplementedError

    async def remove_file(self, path: PurePosixPath) -> None:
        raise NotImplementedError

    async def run_argv(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:
        raise NotImplementedError

    async def open_shell(self) -> RemoteProcess:
        raise NotImplementedError

    async def open_process(
        self, argv: tuple[str, ...], *, term_type: str | None
    ) -> RemoteProcess:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class RemoteTransportFactory(Protocol):
    async def connect(self, target: TargetRegistration) -> RemoteTransport:
        raise NotImplementedError
```

The AsyncSSH adapter wraps its stdin/stdout objects behind `RemoteProcess`. Implement `AsyncSshTransport` with `start_sftp_client()`, `run(shlex.join(argv), encoding=None, check=False, timeout=timeout)`, and `create_process()` for host or fixed-argv container processes. `open_shell()` starts `env PS1= sh`; `open_process()` quotes the supplied typed argv with `shlex.join`. Map library errors into `RemoteConnectionError`, `RemoteTimeoutError`, and `RemotePathError` without exposing key material.

- [ ] **Step 4: Implement `SessionManager` and run focused tests**

`SessionManager.connect(target)` is protected by one `asyncio.Lock` per target, returns the existing live `HostSession`, and otherwise closes stale resources before creating a new UUID session. `disconnect(target_id)` and `close_all()` are idempotent. `HostSession` stores `session_id`, `target_id`, `transport`, `connected_at`, and a lazy `host_process: RemoteProcess | None`; it does not store credentials. Task 4 wraps that process with `PersistentShell`.

`spawn_container_session(host_session_id, container)` always creates a fresh UUID `ContainerSession` and a new process from fixed argv `("docker", "exec", "-i", container, "env", "PS1=", "sh")`. It stores `parent_session_id`, container, and its own `RemoteProcess`; it never changes the HostSession process. Task 4 creates a separate `PersistentShell` wrapper for it. Closing a child closes only that process. Closing or reconnecting a host closes all of its children and marks their state non-restorable.

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run pytest tests/remote_ops/test_asyncssh_adapter.py tests/remote_ops/test_sessions.py -q`

Expected: PASS.

- [ ] **Step 5: Lint and commit**

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/remote_ops tests/remote_ops`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/remote_ops tests/remote_ops
git commit -m "feat: add persistent ssh host sessions"
```

## Task 3: Add Scoped Remote Read, List, Search, and Stat Tools

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/remote_ops/files.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/remote_ops/gateway.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/policy.py`
- Create: `tests/remote_ops/test_files.py`
- Create: `tests/remote_ops/test_gateway.py`

**Interfaces:**
- Consumes: Task 1 scopes and requests, Task 2 `SessionManager`/`RemoteTransport`, Phase 1 `ProjectRegistryStore`.
- Produces: `RemotePathPolicy.authorize()`, `RemoteFileTools.read/list/search/stat`, and `RemoteToolGateway` read-only methods.

- [ ] **Step 1: Write failing path-boundary tests**

```python
from pathlib import PurePosixPath

import pytest
from incidentlens_control_plane.remote_ops.policy import RemotePathDenied, RemotePathPolicy
from incidentlens_control_plane.remote_ops.types import ContainerScope, HostScope


def test_host_path_inside_registered_root_is_allowed(service_registration) -> None:
    policy = RemotePathPolicy(service_registration)
    assert policy.authorize(
        HostScope(), PurePosixPath("/opt/payments/app.py"), write=False
    ) == PurePosixPath("/opt/payments/app.py")


def test_prefix_collision_and_parent_traversal_are_rejected(service_registration) -> None:
    policy = RemotePathPolicy(service_registration)
    with pytest.raises(RemotePathDenied):
        policy.authorize(HostScope(), PurePosixPath("/opt/payments-other/key"), write=False)
    with pytest.raises(RemotePathDenied):
        policy.authorize(HostScope(), PurePosixPath("/opt/payments/../secret"), write=False)


def test_container_scope_uses_container_roots(service_registration) -> None:
    policy = RemotePathPolicy(service_registration)
    assert policy.authorize(
        ContainerScope(container="payments-api-1"),
        PurePosixPath("/app/service.py"),
        write=True,
    ) == PurePosixPath("/app/service.py")
```

Define the fixture in the same test module:

```python
@pytest.fixture
def service_registration() -> ServiceRegistration:
    return ServiceRegistration(
        compose_service="payment-api",
        container_names=("payments-api-1",),
        allowed_host_paths=(PurePosixPath("/opt/payments"),),
        allowed_container_paths=(PurePosixPath("/app"),),
    )
```

- [ ] **Step 2: Write failing bounded file-tool tests**

Use `FakeTransport` preloaded with `/opt/payments/app.py`. Assert `read()` supports byte offset/limit, rejects a response larger than 1 MiB, `list()` returns metadata without file bodies, `search()` returns at most 200 matches with path/line/text, and `stat()` rejects symbolic links until an explicit later policy exists.

```python
result = await tools.read(
    PurePosixPath("/opt/payments/app.py"), offset=0, limit=4096
)
assert result.content == b"print('ok')\n"
assert result.sha256 == hashlib.sha256(result.content).hexdigest()
```

- [ ] **Step 3: Implement lexical and canonical path authorization**

First reject non-absolute paths and `..` components. Use `PurePosixPath.is_relative_to(root)` rather than string-prefix checks. Before reading an existing path, resolve it with transport `realpath()` and re-check the canonical path against the same allowed root. Reject symlinks in Phase 2 to close replacement races. For a new write path, canonicalize and authorize its existing parent.

The selected service comes from `ProjectRegistryStore.get(project_id)` and must exist in that project. The selected target must exist in the same project. A container scope must name one of `ServiceRegistration.container_names`; Phase 4 may add discovered containers through a separately audited registry update. Never accept roots or container authorization from a tool request.

- [ ] **Step 4: Implement bounded tools and gateway methods**

Define results:

```python
class FileReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: PurePosixPath
    content: bytes
    sha256: str
    metadata: FileMetadata
    truncated: bool


class SearchMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: PurePosixPath
    line_number: int = Field(ge=1)
    text: str = Field(max_length=2_000)
```

`RemoteFileTools.search()` walks only through `list_directory()`, skips symlinks, caps traversal at 10,000 files, reads at most 1 MiB per file, and returns at most 200 matches. It never invokes remote `find`, `grep`, or a shell. Gateway methods resolve project/target/service policy, obtain the HostSession, and then call the tools.

At the end of Task 3, host scope is executable. Container scope is policy-validatable but returns `ContainerFileOperationUnsupported` until Task 7 installs the fixed Docker file backend; it must never accidentally interpret `/app` as a host path.

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run pytest tests/remote_ops/test_files.py tests/remote_ops/test_gateway.py -q`

Expected: PASS.

- [ ] **Step 5: Run safety regression, lint, and commit**

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run pytest tests/project_registry tests/remote_ops -q`

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/remote_ops tests/remote_ops`

Expected: both PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/remote_ops tests/remote_ops
git commit -m "feat: add scoped remote read tools"
```

## Task 4: Add Persistent Shell Framing and Command Risk Policy

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/remote_ops/shell.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/policy.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/gateway.py`
- Create: `tests/remote_ops/test_shell.py`
- Expand: `tests/remote_ops/test_policy.py`

**Interfaces:**
- Consumes: `RemoteProcess`, `HostSession`, `ShellRequest`, and `ServiceRegistration` path/container policy.
- Produces: `CommandPolicy.evaluate(request, service) -> PolicyDecision`, `PersistentShell.execute()`, and preliminary `RemoteToolGateway.shell(request)` which executes only automatic commands and returns a canonical approval intent for approval-required commands.

- [ ] **Step 1: Write the permanent-deny matrix**

```python
import pytest
from incidentlens_control_plane.remote_ops.policy import CommandPolicy
from incidentlens_control_plane.remote_ops.types import OperationRisk


@pytest.fixture
def shell_request() -> ShellRequest:
    return ShellRequest(
        operation_id="op-shell",
        incident_id="inc-1",
        project_id="payments",
        target_id="dev-a",
        service="payment-api",
        scope=HostScope(),
        command="pwd",
        reason="inspect current remote directory",
    )


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /opt/app",
        "rm -fr /opt/app",
        "rm -r -f /opt/app",
        "rm --recursive --force /opt/app",
        "sudo rm -R -f /opt/app",
        "command rm -fR /opt/app",
    ],
)
def test_recursive_force_rm_is_always_forbidden(
    command: str, shell_request, service_registration
) -> None:
    decision = CommandPolicy().evaluate(
        shell_request.model_copy(update={"command": command}), service_registration
    )
    assert decision.risk is OperationRisk.FORBIDDEN
    assert decision.approval_can_override is False


@pytest.mark.parametrize(
    "command",
    [
        "docker restart payments-api-1",
        "docker rm payments-api-1",
        "docker compose up -d payment-api",
        "docker compose down",
        "apt-get install strace",
    ],
)
def test_service_or_system_mutation_requires_approval(
    command: str, shell_request, service_registration
) -> None:
    decision = CommandPolicy().evaluate(
        shell_request.model_copy(update={"command": command}), service_registration
    )
    assert decision.risk is OperationRisk.APPROVAL_REQUIRED
```

Also test that `pwd` and `docker ps` are automatic reads. `ls`, `cat`, and `stat` are automatic only when every path argument passes `RemotePathPolicy`; `docker inspect` and `docker logs` are automatic only for a registered container; and `docker compose ps/logs/config` are automatic only for the registered project/service. `sed -i` is rejected with reason "use remote_edit so mandatory backups cannot be bypassed". Unclassified commands require approval.

- [ ] **Step 2: Write persistent PTY tests**

Use a fake byte-stream process. Assert two calls reuse one process, `cd /opt/payments` affects the next `pwd`, a unique random sentinel terminates each response, exit status is parsed, output is capped at 2 MiB, timeout closes the shell, and bytes after a timeout are never attributed to a later command.

```python
first = await shell.execute("cd /opt/payments", timeout=1)
second = await shell.execute("pwd", timeout=1)
assert first.exit_status == 0
assert second.stdout.rstrip().endswith(b"/opt/payments")
assert process.start_count == 1
```

- [ ] **Step 3: Implement conservative command classification**

Split top-level command lists with `shlex` only after rejecting NUL, newlines used to hide additional commands, malformed quoting, command substitution, `eval`, `xargs rm`, and `find -delete`. Strip leading `sudo`, `env NAME=value`, and `command` before identifying the executable. Combine short `rm` flags so every recursive-plus-force combination is permanently forbidden. Validate file arguments through `RemotePathPolicy` and Docker targets through `ServiceRegistration.container_names`; missing or ambiguous arguments require approval.

Return:

```python
class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    risk: OperationRisk
    reason: str
    approval_can_override: bool
    canonical_operation: str
```

The policy is deliberately conservative: parsing uncertainty becomes `APPROVAL_REQUIRED`, never automatic execution. Typed file tools remain the path for edits.

- [ ] **Step 4: Implement framed PTY execution and gateway enforcement**

Serialize calls per shell with an `asyncio.Lock`. For each command, generate a 128-bit marker and write:

```text
<command>
__incidentlens_status=$?
printf '\n__INCIDENTLENS_END_<marker>__:%s\n' "$__incidentlens_status"
```

Read until the exact marker, remove the echoed framing lines, enforce timeout/output limits, and mark the shell unusable after protocol loss. Gateway must evaluate policy before opening the shell. It returns an approval requirement rather than executing `APPROVAL_REQUIRED`, and raises `ForbiddenOperation` for `FORBIDDEN`.

For `HostScope`, a missing `session_id` resolves the reusable host session; a supplied ID must match that target. For `ContainerScope`, `session_id` is mandatory and must resolve to a live `ContainerSession` whose parent target and container equal the request. Wrap each host/container `RemoteProcess` in its own `PersistentShell`, so `cd` and environment changes never leak between parent and child sessions.

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run pytest tests/remote_ops/test_policy.py tests/remote_ops/test_shell.py tests/remote_ops/test_gateway.py -q`

Expected: PASS.

- [ ] **Step 5: Lint and commit**

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/remote_ops tests/remote_ops`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/remote_ops tests/remote_ops
git commit -m "feat: enforce persistent remote shell policy"
```

## Task 5: Persist Exact, Single-Use Approvals

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/approvals/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/approvals/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/approvals/store.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/approvals/service.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/gateway.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/events/types.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Create: `tests/approvals/test_service.py`
- Create: `tests/approvals/test_store.py`

**Interfaces:**
- Consumes: Phase 1 SQLite factory pattern and `RuntimeEventStore`/broker.
- Produces: `ApprovalRequest`, `ApprovalStatus`, `ApprovalStore`, and async `ApprovalService.request/approve/reject/consume`.

- [ ] **Step 1: Write failing approval hash and single-use tests**

```python
from datetime import UTC, datetime

import pytest
from incidentlens_control_plane.approvals.service import (
    ApprovalMismatch,
    ApprovalService,
    ApprovalUnavailable,
)


def test_approval_is_bound_to_canonical_parameters(approval_service: ApprovalService) -> None:
    async def scenario() -> None:
        intent = {
            "kind": "docker.restart",
            "target_id": "dev-a",
            "container": "payments-api-1",
            "argv": ["docker", "restart", "payments-api-1"],
        }
        request = await approval_service.request(
            intent, now=datetime(2026, 8, 10, tzinfo=UTC)
        )
        approved = await approval_service.approve(
            request.approval_id, now=request.created_at
        )

        with pytest.raises(ApprovalMismatch):
            await approval_service.consume(
                approved.approval_id, {**intent, "container": "other"}
            )

        await approval_service.consume(approved.approval_id, intent)
        with pytest.raises(ApprovalUnavailable):
            await approval_service.consume(approved.approval_id, intent)

    asyncio.run(scenario())
```

- [ ] **Step 2: Write persistence and expiry tests**

Create a temporary SQLite database and assert pending/approved/rejected/consumed states survive a new `ApprovalStore` instance. Approval TTL is 15 minutes by default; consuming after `expires_at` raises `ApprovalUnavailable`. Rejected approvals can never be consumed.

```python
def test_expired_approval_cannot_be_consumed(approval_service: ApprovalService) -> None:
    async def scenario() -> None:
        created = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        request = await approval_service.request(INTENT, now=created)
        await approval_service.approve(request.approval_id, now=created)
        with pytest.raises(ApprovalUnavailable):
            await approval_service.consume(
                request.approval_id,
                INTENT,
                now=created + timedelta(minutes=16),
            )

    asyncio.run(scenario())
```

- [ ] **Step 3: Implement canonical hashing and repository**

Canonicalize with sorted compact JSON and hash SHA-256:

```python
def canonical_intent(intent: Mapping[str, JsonValue]) -> bytes:
    return json.dumps(
        intent,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def intent_sha256(intent: Mapping[str, JsonValue]) -> str:
    return hashlib.sha256(canonical_intent(intent)).hexdigest()
```

`ApprovalRequest` contains `approval_id`, `intent_sha256`, redacted `intent_summary`, status, `created_at`, `expires_at`, `decided_at`, and `consumed_at`. Store the full canonical intent locally but never emit secrets in Runtime events. Use a SQLite transaction with `UPDATE ... WHERE status='approved' AND consumed_at IS NULL` to guarantee one-time consumption. Test fixtures construct `ApprovalStore` and `RuntimeEventStore` on the same temporary SQLite database plus a `RuntimeEventBroker`, run both migrations, and pass them into `ApprovalService`.

- [ ] **Step 4: Emit durable approval events and construct the service**

Add `APPROVAL_REQUESTED`, `APPROVAL_APPROVED`, `APPROVAL_REJECTED`, and `APPROVAL_CONSUMED` to `RuntimeEventType`. `ApprovalService` appends then publishes events containing approval ID, operation kind, target, and status. Add `approvals: ApprovalService` to `RuntimeServices` and run its migration in `build_runtime()`.

Update the gateway signature to `shell(request: ShellRequest, approval_id: str | None = None)`. Automatic commands execute immediately. Approval-required commands call `await approvals.request(canonical_intent)` when no ID is provided and return that pending record without transport execution; with an ID, call `await approvals.consume(approval_id, canonical_intent)` immediately before writing to the PTY. Forbidden commands never create approvals.

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run pytest tests/approvals tests/events tests/test_app.py -q`

Expected: PASS.

- [ ] **Step 5: Lint and commit**

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/approvals apps/control-plane/src/incidentlens_control_plane/events apps/control-plane/src/incidentlens_control_plane/runtime.py tests/approvals`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/approvals apps/control-plane/src/incidentlens_control_plane/events apps/control-plane/src/incidentlens_control_plane/remote_ops/gateway.py apps/control-plane/src/incidentlens_control_plane/runtime.py tests/approvals
git commit -m "feat: add exact single-use approvals"
```

## Task 6: Add ChangeSets and Encrypted Local Backup Vault

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/changes/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/changes/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/changes/store.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/changes/backup.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/events/types.py`
- Create: `tests/changes/test_store.py`
- Create: `tests/changes/test_backup.py`

**Interfaces:**
- Consumes: `RuntimeSettings.data_dir`, SQLite connection factory, Runtime events.
- Produces: `FileChange`, `ChangeSet`, `ChangeSetStatus`, `ChangeSetStore`, `BackupReference`, and `EncryptedBackupVault.store/load`.

- [ ] **Step 1: Write failing encrypted backup tests**

```python
from pathlib import PurePosixPath

from incidentlens_control_plane.changes.backup import EncryptedBackupVault


def test_backup_is_encrypted_and_round_trips(tmp_path) -> None:
    vault = EncryptedBackupVault(tmp_path / "backups", tmp_path / "backup.key")
    original = b"DATABASE_PASSWORD=super-secret\n"

    reference = vault.store(
        target_id="dev-a",
        incident_id="inc-1",
        changeset_id="chg-1",
        remote_path=PurePosixPath("/opt/payments/.env"),
        content=original,
    )

    assert reference.sha256 == hashlib.sha256(original).hexdigest()
    assert reference.local_path.read_bytes() != original
    assert vault.load(reference) == original
    assert stat.S_IMODE(reference.local_path.stat().st_mode) == 0o600
```

Also assert the key file is mode `0o600`, paths are encoded below the vault root without traversal, and ciphertext tampering raises `BackupIntegrityError`.

- [ ] **Step 2: Write ChangeSet transition tests**

```python
def test_changeset_cannot_apply_before_both_backups(store, draft_change) -> None:
    changeset = store.create(draft_change)
    store.transition(changeset.changeset_id, ChangeSetStatus.PREFLIGHTED)
    store.transition(changeset.changeset_id, ChangeSetStatus.LOCALLY_BACKED_UP)

    with pytest.raises(InvalidChangeTransition):
        store.transition(changeset.changeset_id, ChangeSetStatus.APPLIED)
```

Cover the exact state graph `draft -> preflighted -> locally_backed_up -> remotely_backed_up -> applied -> validated -> verified|failed|rolled_back`. Permit `failed` from any active state and `rolled_back` only after at least one file was applied.

- [ ] **Step 3: Implement immutable change values and SQLite journal**

`FileChange` contains scope, path, expected original SHA-256 or `None` for a new file, replacement bytes SHA-256, UTF-8 diff for display, original metadata, local backup reference, remote backup path, temporary path, applied flag, validation result, and rollback result. Store replacement content in the encrypted vault, not SQLite.

`ChangeSet` contains ID, incident/project/target/service, tuple of files, status, timestamps, verification plan, rollback plan, and optional approval ID. Every transition uses a SQLite transaction and compares the current status to the allowed predecessor set.

- [ ] **Step 4: Implement AES-GCM backup storage**

Generate a 256-bit key once with `AESGCM.generate_key(bit_length=256)` and atomically create the key file with mode `0o600`. Encrypt every backup with a random 12-byte nonce and authenticated metadata containing target, incident, ChangeSet, remote path, and plaintext SHA-256. Store `nonce + ciphertext` under the data directory using sanitized ID segments and a SHA-256 encoding of the absolute remote path.

Add `CHANGESET_CREATED`, `CHANGESET_STATUS_CHANGED`, and `CHANGESET_ROLLED_BACK` Runtime event kinds.

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run pytest tests/changes tests/events -q`

Expected: PASS.

- [ ] **Step 5: Lint and commit**

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/changes tests/changes`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/changes apps/control-plane/src/incidentlens_control_plane/events tests/changes
git commit -m "feat: journal changes and encrypt backups"
```

## Task 7: Apply Atomic Host and Container File Changes

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/changes/manager.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/gateway.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/transport.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/asyncssh_adapter.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Create: `tests/changes/test_manager.py`
- Expand: `tests/remote_ops/fakes.py`

**Interfaces:**
- Consumes: Task 2 transport, Task 3 path policy, Task 5 approvals, Task 6 store/vault.
- Produces: `ChangeManager.apply(request: ChangeSetRequest)`, `.verify(changeset_id, result)`, `.rollback(changeset_id, approval_id)`, gateway `edit/write/apply_changeset/restore`, and `RemoteToolGateway.docker_action(request, approval_id)`.

- [ ] **Step 1: Write the mandatory two-backup ordering test**

```python
def test_edit_backs_up_locally_and_remotely_before_replace(change_manager, fake_transport):
    result = asyncio.run(
        change_manager.apply(single_file_changeset("/opt/payments/app.py"))
    )

    assert fake_transport.calls == [
        "lstat:/opt/payments/app.py",
        "realpath:/opt/payments/app.py",
        "read:/opt/payments/app.py",
        "copy:/opt/payments/app.py:/opt/payments/app.py.incidentlens-backup.20260810T120000.000000Z",
        "read:/opt/payments/app.py.incidentlens-backup.20260810T120000.000000Z",
        "write:/opt/payments/.app.py.incidentlens-tmp-chg-1",
        "rename:/opt/payments/.app.py.incidentlens-tmp-chg-1:/opt/payments/app.py",
    ]
    assert result.status is ChangeSetStatus.APPLIED
```

Assert no write happens when local backup fails, remote `cp --preserve` fails, backup hash differs, source hash is stale, target is a symlink, or the temporary filename already exists.

- [ ] **Step 2: Write multi-file failure and rollback tests**

Use two files. Fail the second rename and assert the first is restored from its verified remote backup, the second original remains untouched, status becomes `rolled_back`, and no recursive delete command is used. Also assert a new file is removed only with the typed single-file `remove_file()` operation during rollback.

```python
def test_second_rename_failure_restores_first_file(change_manager, fake_transport):
    fake_transport.fail_rename_for = PurePosixPath("/opt/payments/b.py")
    result = asyncio.run(change_manager.apply(two_file_edit_request()))

    assert result.status is ChangeSetStatus.ROLLED_BACK
    assert fake_transport.files[PurePosixPath("/opt/payments/a.py")] == b"old-a\n"
    assert fake_transport.files[PurePosixPath("/opt/payments/b.py")] == b"old-b\n"
    assert all("rm -r" not in call and "rm -fR" not in call for call in fake_transport.calls)
```

Define the helpers in the test module:

```python
def edit(path: str, original: bytes, old: str, new: str) -> FileEditRequest:
    return FileEditRequest(
        operation_id=f"op-{PurePosixPath(path).name}",
        incident_id="inc-1",
        project_id="payments",
        target_id="dev-a",
        service="payment-api",
        scope=HostScope(),
        path=PurePosixPath(path),
        expected_sha256=hashlib.sha256(original).hexdigest(),
        replacements=(TextReplacement(old_text=old, new_text=new),),
    )


def changeset(*files: FileEditRequest) -> ChangeSetRequest:
    return ChangeSetRequest(
        changeset_id="chg-1",
        files=files,
        verification_plan="run syntax checks and compare service behavior",
        rollback_plan="restore both verified timestamped backups",
    )


def single_file_changeset(path: str) -> ChangeSetRequest:
    return changeset(edit(path, b"old\n", "old", "new"))


def two_file_edit_request() -> ChangeSetRequest:
    return changeset(
        edit("/opt/payments/a.py", b"old-a\n", "old-a", "new-a"),
        edit("/opt/payments/b.py", b"old-b\n", "old-b", "new-b"),
    )
```

- [ ] **Step 3: Implement host and container file backend operations**

For host scope, use SFTP for bytes/stat/rename/remove. Add `copy_file(source, target, preserve=True)` to the transport; AsyncSSH implements it with the fixed argv tuple `("cp", "--preserve", "--", str(source), str(target))`, never a model-provided command.

For container scope, use fixed `docker exec` argv templates for `cat`, `stat`, `cp --preserve`, `chmod`, and `mv`. Stream new content to a randomized host temporary file via SFTP, copy it into the container with fixed `docker cp`, then remove only that exact host file through SFTP. Validate container names with the Task 1 pattern and insert `--` where the Docker command supports it. If required utilities are absent, return `ContainerFileOperationUnsupported`; never fall back to generated Python or shell scripts.

- [ ] **Step 4: Implement the ChangeManager transaction**

Under one lock keyed by `(target_id, scope, path)`, perform:

1. Re-authorize and canonicalize every path.
2. Read current bytes/metadata and compare expected SHA-256.
3. For `FileEditRequest`, decode UTF-8, locate every replacement against the original text, require exactly `expected_count`, reject overlapping ranges, and apply replacements from highest offset to lowest.
4. Validate generated `.py` with `ast.parse`, `.json` with `json.loads`, and `.toml` with `tomllib.loads` before any remote backup or write. Unsupported formats remain explicitly `not_validated`, not falsely successful.
5. Store and verify every encrypted local backup.
6. Create every same-directory `<name>.incidentlens-backup.<UTC timestamp>` remote backup and verify its bytes.
7. Write every randomized same-directory temporary file with original mode.
8. For Compose YAML, run fixed `docker compose -f <temporary-path> config -q` after upload and before replacement.
9. Recheck each original SHA-256 immediately before the first rename.
10. Rename files in deterministic path order.
11. On failure, restore already-applied files in reverse order.
12. Persist every state change and emit Runtime events.

For `FileWriteRequest(expected_sha256=None)`, require the target not to exist. Record an authenticated "originally absent" marker in the local vault instead of inventing backup bytes; rollback removes only that exact newly created file through `remove_file()`. If the target exists, reject the request and require a new request carrying its SHA-256 so both backups are created.

Acquire all per-path locks in sorted path order before preflight and release them in reverse order to prevent overlapping ChangeSets and deadlocks. Gateway `edit()` and `write()` wrap one request in a generated `ChangeSetRequest`; `apply_changeset()` accepts the explicit multi-file request.

Ordinary files need no human approval. A path matching `protected_remote_paths`, `.env`, `compose.yaml`, `compose.yml`, `docker-compose*.yml`, `Dockerfile`, `/etc`, or a systemd unit requires an exact approval consumed immediately before the first rename.

Implement `docker_action()` as a separate typed path. It resolves the registered target/service, verifies a container action names a registered container, builds a fixed argv tuple, requests approval when none is supplied, and consumes the exact approval immediately before transport execution. Container argv is `docker <stop|restart|kill|rm> -- <container>`. Compose argv includes the registered `--project-directory` and optional `--project-name`, then uses `stop`, `restart`, `down`, or `up -d`; it never accepts these values from the model. Persist and emit requested/started/completed/failed events.

After Task 7, `RuntimeServices` has this exact shape:

```python
@dataclass(frozen=True, slots=True)
class RuntimeServices:
    projects: ProjectRegistryStore
    events: RuntimeEventStore
    broker: RuntimeEventBroker
    sessions: SessionManager
    approvals: ApprovalService
    change_store: ChangeSetStore
    backups: EncryptedBackupVault
    changes: ChangeManager
    remote_tools: RemoteToolGateway
```

`build_runtime()` constructs these in dependency order. The application lifespan calls `await services.sessions.close_all()` before clearing `app.state.runtime`; it does not reconnect sessions after a Runtime restart.

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run pytest tests/changes/test_manager.py tests/remote_ops -q`

Expected: PASS.

- [ ] **Step 5: Lint and commit**

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane/changes apps/control-plane/src/incidentlens_control_plane/remote_ops tests/changes tests/remote_ops`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/changes apps/control-plane/src/incidentlens_control_plane/remote_ops apps/control-plane/src/incidentlens_control_plane/runtime.py tests/changes tests/remote_ops
git commit -m "feat: apply reversible remote changes"
```

## Task 8: Expose Remote Sessions, Approvals, and Change Recovery

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/routes/remote_sessions.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/routes/approvals.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/routes/changes.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/events/types.py`
- Create: `tests/web/test_remote_sessions_api.py`
- Create: `tests/web/test_approvals_api.py`
- Create: `tests/web/test_changes_api.py`

**Interfaces:**
- Consumes: `SessionManager`, `ApprovalService`, `ChangeManager`, stores and event broker.
- Produces: local APIs for connection lifecycle, approval decisions, ChangeSet inspection, verification, and rollback.

- [ ] **Step 1: Write failing remote-session API tests with injected fake transport**

```python
def test_connect_reuses_session_and_never_returns_credentials(client, registered_project):
    first = client.post("/api/remote-sessions", json={
        "project_id": "payments",
        "target_id": "dev-a",
    })
    second = client.post("/api/remote-sessions", json={
        "project_id": "payments",
        "target_id": "dev-a",
    })

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["session_id"] == second.json()["session_id"]
    assert "credential" not in first.text
    assert "private" not in first.text


def test_container_session_is_fresh_and_parent_scoped(client, connected_host):
    body = {"project_id": "payments", "service": "payment-api", "container": "payments-api-1"}
    first = client.post(
        f"/api/remote-sessions/{connected_host}/containers", json=body
    )
    second = client.post(
        f"/api/remote-sessions/{connected_host}/containers", json=body
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["session_id"] != second.json()["session_id"]
    assert first.json()["parent_session_id"] == connected_host
```

Test unknown projects/targets return 404, failed host-key/authentication returns a redacted 502, `DELETE /api/remote-sessions/{session_id}` is idempotent, and status contains connection health but no transport object.

- [ ] **Step 2: Write approval and recovery API tests**

Assert:

- `GET /api/approvals?status=pending` returns pending requests.
- `POST /api/approvals/{id}/approve` and `/reject` transition once; repeats return 409.
- `GET /api/changes/{id}` returns diff, backup references, validation, and rollback status but not backup plaintext.
- `POST /api/changes/{id}/verify` accepts a structured result and moves `applied -> validated -> verified|failed`.
- `POST /api/changes/{id}/rollback` requires an approval when rollback interrupts a service and returns 202 while executing.

```python
def test_approval_decision_is_single_use(client, pending_approval) -> None:
    approved = client.post(f"/api/approvals/{pending_approval}/approve")
    repeated = client.post(f"/api/approvals/{pending_approval}/approve")
    assert approved.status_code == 200
    assert repeated.status_code == 409


def test_changes_api_never_returns_backup_plaintext(client, applied_changeset) -> None:
    response = client.get(f"/api/changes/{applied_changeset}")
    assert response.status_code == 200
    assert "DATABASE_PASSWORD" not in response.text
    assert response.json()["status"] == "applied"
```

- [ ] **Step 3: Implement request/response schemas and routes**

Use separate API schemas so domain objects containing canonical intents or local encrypted paths are not serialized accidentally. Connect by resolving the target from `ProjectRegistryStore`; never accept host, username, port, allowed paths, or credentials from the connect request. `POST /api/remote-sessions/{host_session_id}/containers` resolves the registered service, requires the requested container in `container_names`, and always creates a new child session. Deleting a child does not disconnect the host. Register all routers in `create_app()`.

Add event kinds `REMOTE_SESSION_CONNECTED`, `REMOTE_SESSION_DISCONNECTED`, `REMOTE_SESSION_FAILED`, `REMOTE_OPERATION_STARTED`, and `REMOTE_OPERATION_COMPLETED`. Payloads contain IDs, tool kind, target/service, risk, duration, and result reference only.

- [ ] **Step 4: Run API and lifecycle tests**

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run pytest tests/web tests/approvals tests/changes tests/remote_ops tests/test_app.py -q`

Expected: PASS.

- [ ] **Step 5: Lint and commit**

Run: `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run ruff check apps/control-plane/src/incidentlens_control_plane tests/web`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/routes apps/control-plane/src/incidentlens_control_plane/main.py apps/control-plane/src/incidentlens_control_plane/events/types.py tests/web
git commit -m "feat: expose safe remote operation api"
```

## Task 9: Verify Against a Disposable Docker SSH Target

**Files:**
- Create: `infra/test-ssh/Dockerfile`
- Create: `infra/test-ssh/compose.yaml`
- Create: `tests/integration/test_live_ssh_tools.py`
- Create: `docs/phase-2-remote-tools-verification.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: all Phase 2 public APIs and AsyncSSH adapter.
- Produces: reproducible live verification; no new production interface.

- [ ] **Step 1: Create the disposable SSH fixture**

Use `python:3.12-slim`, install `openssh-server`, create non-root user `incidentlens`, create `/workspace/service`, and configure public-key-only authentication. `compose.yaml` mounts a test-generated `authorized_keys` file read-only and publishes container port 22 to `127.0.0.1` on an ephemeral host port. Do not commit a private key.

The pytest fixture must:

```python
key_path = tmp_path / "id_ed25519"
subprocess.run(
    ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
    check=True,
)
subprocess.run(
    ["docker", "compose", "-f", str(compose_file), "up", "-d", "--build"],
    check=True,
    env={**os.environ, "TEST_AUTHORIZED_KEYS": str(key_path.with_suffix(".pub"))},
)
```

Register cleanup with `request.addfinalizer`; use `docker compose down --volumes` only against the explicit test compose project. Skip with a clear reason when Docker or `ssh-keygen` is unavailable.

- [ ] **Step 2: Write the live acceptance test**

Connect using the generated key supplied to `AsyncSshTransportFactory` through a test-only constructor option. Verify:

1. Two `SessionManager.connect()` calls reuse one session ID.
2. Persistent shell `cd /workspace/service` affects the following `pwd`.
3. Read/list/stat return bounded results.
4. Editing a large Python file with at least three non-adjacent replacements creates encrypted local and timestamped remote backups before replacement.
5. A stale hash blocks a second edit.
6. `rm -rf` is rejected without contacting the transport.
7. `docker restart` produces an approval request and is not executed before approval.
8. A forced validation failure restores original bytes.

- [ ] **Step 3: Run unit checks and the opt-in live test**

Run:

```bash
UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run pytest -q
UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run ruff check .
INCIDENTLENS_RUN_LIVE_SSH=1 UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run pytest tests/integration/test_live_ssh_tools.py -q
```

Expected: unit tests and Ruff pass; the live test passes when Docker is available and otherwise skips only when the opt-in variable is absent.

- [ ] **Step 4: Document exact manual verification and recovery**

In `docs/phase-2-remote-tools-verification.md`, include registration JSON with explicit allowed roots, connection commands, read/edit examples, where to find the remote timestamp backup, how to inspect encrypted local backup metadata, the approval flow for Docker restart/rm, rollback, disconnect, and cleanup. State prominently that this is a disposable target and that production testing must use a non-critical server first.

Update README to describe Phase 2 capabilities and retain the statement that no server-side IncidentLens agent is installed.

- [ ] **Step 5: Commit documentation and integration fixture**

```bash
git add infra/test-ssh tests/integration/test_live_ssh_tools.py docs/phase-2-remote-tools-verification.md README.md
git commit -m "test: verify persistent safe ssh tools"
```

## Approved Specification Coverage

| Phase 2 requirement | Implemented by |
|---|---|
| Existing local SSH configuration, key agent, known-host verification | Tasks 1-2 |
| One persistent connection with SFTP, command, and PTY channels | Tasks 2 and 4 |
| Host/container Read, List, Search, Stat | Task 3 and Task 7 container adapter |
| Large multi-location Edit/Write without editor scripts or manual transfer | Tasks 1, 3, and 7 |
| Current-directory and shell-state persistence | Task 4 |
| Permanent recursive-force `rm` denial | Task 4 |
| Exact approval for Docker/service-impacting operations | Tasks 4, 5, 7, and 8 |
| Encrypted local plus timestamped remote backup | Tasks 6-7 |
| Stale-write detection, atomic replace, multi-file rollback | Task 7 |
| Durable audit events and local inspection APIs | Tasks 5, 6, and 8 |
| Real SSH verification and lifecycle cleanup | Task 9 |

## Phase Completion Gate

Before writing the Phase 3 log implementation plan, verify:

- `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run pytest -q` passes.
- `UV_CACHE_DIR=/private/tmp/incidentlens-uv-cache uv run ruff check .` passes.
- The opt-in Docker SSH acceptance test passes.
- One registered target uses one reusable SSH connection with multiple channels.
- The API and events never expose credentials, connection objects, backup plaintext, or unredacted canonical intents.
- Host and container paths cannot escape registered roots through `..`, prefix collisions, or symlinks.
- Large multi-location edits require neither `vi`, `sed`, a generated Python script, nor manual `scp`.
- Every overwrite has a verified encrypted local backup and verified timestamped same-directory remote backup.
- `rm -rf` variants are permanently denied before transport execution.
- Docker service interruption and deletion do not execute without an exact, unexpired, single-use approval.
- Failed multi-file changes restore already-applied files and retain evidence of the failure.
- Runtime shutdown closes PTY, SFTP, and SSH resources cleanly.
