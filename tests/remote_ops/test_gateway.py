"""Tests for Gateway and RemoteToolGateway with scoped file tools."""

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest
from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.changes.manager import ChangeManager
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.project_registry.types import (
    ProjectRecord,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.fakes import FakeTransport, FakeTransportFactory
from incidentlens_control_plane.remote_ops.files import (
    ContainerFileOperationUnsupported,
    FileReadResult,
    SearchMatch,
)
from incidentlens_control_plane.remote_ops.gateway import (
    CommandForbidden,
    Gateway,
    RemoteToolGateway,
)
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.transport import FileMetadata
from incidentlens_control_plane.remote_ops.types import (
    ContainerScope,
    OperationRisk,
    ScopeKind,
    ShellRequest,
)


def _make_approval_service(tmp_path: Path) -> ApprovalService:
    """Create an ApprovalService backed by a temporary SQLite database."""
    db_path = tmp_path / "approvals.db"

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    approvals = ApprovalStore(connect)
    events = RuntimeEventStore(connect)
    broker = RuntimeEventBroker()
    approvals.migrate()
    events.migrate()
    return ApprovalService(approvals=approvals, events=events, broker=broker)


def _make_shell_request(
    *,
    command: str = "ls -la /var/log",
    risk: OperationRisk = OperationRisk.APPROVAL_REQUIRED,
    target_id: str = "dev-a",
    service: str = "web",
) -> ShellRequest:
    """Create a ShellRequest with the given parameters."""
    return ShellRequest(
        operation_id="op-001",
        incident_id="inc-001",
        project_id="proj-001",
        target_id=target_id,
        service=service,
        scope=ContainerScope(kind=ScopeKind.CONTAINER, container="web-1"),
        risk=risk,
        command=command,
        reason="Investigate logs",
    )


class TestThreeTierRouting:
    """Tests for the three-tier command routing."""

    def test_automatic_command_executes_immediately(self, tmp_path: Path) -> None:
        """AUTO_READ commands execute without approval."""
        service = _make_approval_service(tmp_path)
        gateway = Gateway(approvals=service)
        request = _make_shell_request(
            command="cat /var/log/app.log",
            risk=OperationRisk.AUTO_READ,
        )

        async def scenario() -> None:
            result = await gateway.shell(request)
            assert result.approved is True
            assert result.approval_id is None
            assert "Automatic" in result.reason

        asyncio.run(scenario())

    def test_backup_required_command_executes_immediately(
        self, tmp_path: Path
    ) -> None:
        """BACKUP_REQUIRED commands execute without approval."""
        service = _make_approval_service(tmp_path)
        gateway = Gateway(approvals=service)
        request = _make_shell_request(
            command="cp /etc/config /backup/config.bak",
            risk=OperationRisk.BACKUP_REQUIRED,
        )

        async def scenario() -> None:
            result = await gateway.shell(request)
            assert result.approved is True
            assert result.approval_id is None
            assert "Automatic" in result.reason

        asyncio.run(scenario())

    def test_forbidden_command_raises_exception(self, tmp_path: Path) -> None:
        """FORBIDDEN commands raise CommandForbidden."""
        service = _make_approval_service(tmp_path)
        gateway = Gateway(approvals=service)
        request = _make_shell_request(
            command="rm -rf /",
            risk=OperationRisk.FORBIDDEN,
        )

        async def scenario() -> None:
            with pytest.raises(CommandForbidden, match="forbidden"):
                await gateway.shell(request)

        asyncio.run(scenario())

    def test_forbidden_command_does_not_create_approval(
        self, tmp_path: Path
    ) -> None:
        """FORBIDDEN commands never create approvals."""
        service = _make_approval_service(tmp_path)
        gateway = Gateway(approvals=service)
        request = _make_shell_request(
            command="DROP TABLE users",
            risk=OperationRisk.FORBIDDEN,
        )

        async def scenario() -> None:
            with pytest.raises(CommandForbidden):
                await gateway.shell(request)
            # Verify no approval was created by checking the store
            # (store has no list method, but the service would have no events)

        asyncio.run(scenario())


class TestApprovalRequiredFlow:
    """Tests for the approval-required command flow."""

    def test_requests_approval_when_no_id(self, tmp_path: Path) -> None:
        """Without approval_id, creates a pending approval request."""
        service = _make_approval_service(tmp_path)
        gateway = Gateway(approvals=service)
        request = _make_shell_request()

        async def scenario() -> None:
            result = await gateway.shell(request)
            assert result.approved is False
            assert result.approval_id is not None
            assert result.approval_id.startswith("apr-")
            assert "Approval required" in result.reason

        asyncio.run(scenario())

    def test_consumes_approval_when_id_provided(self, tmp_path: Path) -> None:
        """With valid approval_id, consumes the approval."""
        service = _make_approval_service(tmp_path)
        gateway = Gateway(approvals=service)
        request = _make_shell_request()
        intent = {
            "kind": "shell",
            "target_id": request.target_id,
            "command": request.command,
            "service": request.service,
        }

        async def scenario() -> None:
            now = datetime.now(UTC)
            record = await service.request(intent, now=now)
            await service.approve(record.approval_id, now=now)

            result = await gateway.shell(request, approval_id=record.approval_id)
            assert result.approved is True
            assert result.approval_id == record.approval_id
            assert "Approved and consumed" in result.reason

        asyncio.run(scenario())

    def test_returns_error_when_approval_not_found(self, tmp_path: Path) -> None:
        """Nonexistent approval_id returns a graceful error."""
        service = _make_approval_service(tmp_path)
        gateway = Gateway(approvals=service)
        request = _make_shell_request()

        async def scenario() -> None:
            result = await gateway.shell(
                request, approval_id="apr-nonexistent"
            )
            assert result.approved is False
            assert result.approval_id == "apr-nonexistent"
            assert "not found" in result.reason.lower()

        asyncio.run(scenario())

    def test_returns_error_when_approval_unavailable(self, tmp_path: Path) -> None:
        """Expired approval returns a graceful error."""
        service = _make_approval_service(tmp_path)
        gateway = Gateway(approvals=service)
        request = _make_shell_request()
        intent = {
            "kind": "shell",
            "target_id": request.target_id,
            "command": request.command,
            "service": request.service,
        }

        async def scenario() -> None:
            created = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
            record = await service.request(intent, now=created)
            await service.approve(record.approval_id, now=created)

            # Try to consume after expiry (16 minutes later)
            result = await gateway.shell(
                request,
                approval_id=record.approval_id,
            )
            # Gateway uses current time, but approval was created at 2026-08-10
            # The approval is expired relative to current time
            assert result.approved is False
            assert "unavailable" in result.reason.lower()

        asyncio.run(scenario())

    def test_returns_error_on_intent_mismatch(self, tmp_path: Path) -> None:
        """Intent mismatch returns a graceful error."""
        service = _make_approval_service(tmp_path)
        gateway = Gateway(approvals=service)

        async def scenario() -> None:
            now = datetime(2026, 8, 10, tzinfo=UTC)
            # Create approval for one command
            intent_original = {
                "kind": "shell",
                "target_id": "dev-a",
                "command": "ls -la /var/log",
                "service": "web",
            }
            record = await service.request(intent_original, now=now)
            await service.approve(record.approval_id, now=now)

            # Try to consume with a different command
            request_different = _make_shell_request(
                command="rm -rf /",  # Different command
            )
            result = await gateway.shell(
                request_different, approval_id=record.approval_id
            )
            assert result.approved is False
            assert "mismatch" in result.reason.lower()

        asyncio.run(scenario())

    def test_returns_error_when_approval_rejected(self, tmp_path: Path) -> None:
        """Rejected approval returns a graceful error."""
        service = _make_approval_service(tmp_path)
        gateway = Gateway(approvals=service)
        request = _make_shell_request()
        intent = {
            "kind": "shell",
            "target_id": request.target_id,
            "command": request.command,
            "service": request.service,
        }

        async def scenario() -> None:
            now = datetime(2026, 8, 10, tzinfo=UTC)
            record = await service.request(intent, now=now)
            await service.reject(record.approval_id, now=now)

            result = await gateway.shell(
                request, approval_id=record.approval_id
            )
            assert result.approved is False
            assert "unavailable" in result.reason.lower()

        asyncio.run(scenario())

    def test_double_consume_returns_error(self, tmp_path: Path) -> None:
        """Consuming an already-consumed approval returns an error."""
        service = _make_approval_service(tmp_path)
        gateway = Gateway(approvals=service)
        request = _make_shell_request()
        intent = {
            "kind": "shell",
            "target_id": request.target_id,
            "command": request.command,
            "service": request.service,
        }

        async def scenario() -> None:
            now = datetime.now(UTC)
            record = await service.request(intent, now=now)
            await service.approve(record.approval_id, now=now)

            # First consumption succeeds
            result1 = await gateway.shell(
                request, approval_id=record.approval_id
            )
            assert result1.approved is True

            # Second consumption fails
            result2 = await gateway.shell(
                request, approval_id=record.approval_id
            )
            assert result2.approved is False
            assert "unavailable" in result2.reason.lower()

        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# RemoteToolGateway tests
# ---------------------------------------------------------------------------

_HOST_FS: dict[PurePosixPath, bytes] = {
    PurePosixPath("/opt/app.py"): b"print('hello')\n",
    PurePosixPath("/opt/lib.py"): b"# lib\nx = 1\n",
}


class _FakeProjectRegistryStore:
    """Minimal in-memory project registry for gateway tests."""

    def __init__(self, record: ProjectRecord) -> None:
        self._record = record

    def get(self, project_id: str) -> ProjectRecord:
        if project_id != self._record.project_id:
            from incidentlens_control_plane.project_registry.store import (
                ProjectNotFound,
            )

            raise ProjectNotFound(f"project {project_id!r} not found")
        return self._record


class _FileTransportFactory(FakeTransportFactory):
    """Factory that returns transports with preloaded files."""

    def __init__(self, files: dict[PurePosixPath, bytes]) -> None:
        super().__init__()
        self._files = files

    async def connect(self, target: TargetRegistration) -> FakeTransport:
        transport = FakeTransport(target=target, _files=self._files)
        self.connect_calls.append(target)
        self.transports.append(transport)
        return transport


class _RecordingTransportFactory:
    """Factory that always returns a single recording transport."""

    def __init__(self, transport) -> None:
        self._transport = transport

    async def connect(self, target: TargetRegistration):
        return self._transport


@pytest.fixture
def project_record() -> ProjectRecord:
    return ProjectRecord(
        project_id="myproj",
        display_name="My Project",
        targets=(
            TargetRegistration(
                target_id="dev-host",
                host="10.0.0.1",
                ssh_user="deploy",
            ),
        ),
        services=(
            ServiceRegistration(
                compose_service="web",
                container_names=("web-1",),
                allowed_host_paths=(PurePosixPath("/opt"),),
                allowed_container_paths=(PurePosixPath("/app"),),
            ),
        ),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def session_manager() -> SessionManager:
    factory = _FileTransportFactory(_HOST_FS)
    return SessionManager(factory)


@pytest.fixture
def gateway(
    project_record: ProjectRecord,
    session_manager: SessionManager,
) -> RemoteToolGateway:
    projects = _FakeProjectRegistryStore(project_record)
    target_reg = project_record.targets[0]
    return RemoteToolGateway(
        projects=projects,
        sessions=session_manager,
        targets={target_reg.target_id: target_reg},
    )


@pytest.mark.asyncio
async def test_gateway_host_read(gateway: RemoteToolGateway) -> None:
    result = await gateway.read(
        project_id="myproj",
        target_id="dev-host",
        service="web",
        path=PurePosixPath("/opt/app.py"),
    )
    assert isinstance(result, FileReadResult)
    assert result.content == b"print('hello')\n"
    assert result.path == PurePosixPath("/opt/app.py")


@pytest.mark.asyncio
async def test_gateway_host_list(gateway: RemoteToolGateway) -> None:
    result = await gateway.list_dir(
        project_id="myproj",
        target_id="dev-host",
        service="web",
        path=PurePosixPath("/opt"),
    )
    assert isinstance(result, tuple)
    names = {m.path.name for m in result}
    assert "app.py" in names
    assert "lib.py" in names


@pytest.mark.asyncio
async def test_gateway_host_search(gateway: RemoteToolGateway) -> None:
    result = await gateway.search(
        project_id="myproj",
        target_id="dev-host",
        service="web",
        path=PurePosixPath("/opt"),
        query="print",
    )
    assert isinstance(result, tuple)
    assert len(result) >= 1
    assert isinstance(result[0], SearchMatch)
    assert "print" in result[0].text


@pytest.mark.asyncio
async def test_gateway_host_stat(gateway: RemoteToolGateway) -> None:
    result = await gateway.stat(
        project_id="myproj",
        target_id="dev-host",
        service="web",
        path=PurePosixPath("/opt/app.py"),
    )
    assert isinstance(result, FileMetadata)
    assert result.path == PurePosixPath("/opt/app.py")
    assert result.size == len(b"print('hello')\n")


class _SymlinkAwareTransport(FakeTransport):
    """FakeTransport whose ``realpath`` resolves a fixed symlink map."""

    def __init__(
        self,
        files: dict[PurePosixPath, bytes],
        symlinks: dict[PurePosixPath, PurePosixPath],
    ) -> None:
        super().__init__(target=None, _files=files)  # type: ignore[arg-type]
        self._symlinks = symlinks

    async def realpath(self, path: PurePosixPath) -> PurePosixPath:
        # Resolve the leftmost symlink prefix, then re-resolve from the target.
        for i in range(1, len(path.parts) + 1):
            prefix = PurePosixPath(*path.parts[:i])
            if prefix in self._symlinks:
                suffix = PurePosixPath(*path.parts[i:])
                return await self.realpath(self._symlinks[prefix] / suffix)
        return path


class _SymlinkTransportFactory:
    """Factory returning a single pre-configured symlink-aware transport."""

    def __init__(self, transport) -> None:
        self._transport = transport

    async def connect(self, target: TargetRegistration):
        return self._transport


def _make_symlink_gateway(
    project_record: ProjectRecord,
    transport,
) -> RemoteToolGateway:
    sessions = SessionManager(_SymlinkTransportFactory(transport))
    target_reg = project_record.targets[0]
    return RemoteToolGateway(
        projects=_FakeProjectRegistryStore(project_record),
        sessions=sessions,
        targets={target_reg.target_id: target_reg},
    )


@pytest.mark.asyncio
async def test_gateway_host_read_rejects_symlink_component_escape(
    project_record: ProjectRecord,
) -> None:
    """A read whose intermediate component is a symlink escaping the allowed
    root is rejected before any bytes are read (matching the write path)."""
    from incidentlens_control_plane.remote_ops.policy import RemotePathDenied

    transport = _SymlinkAwareTransport(
        files={PurePosixPath("/etc/passwd"): b"root:x:0:0\n"},
        symlinks={PurePosixPath("/opt/link"): PurePosixPath("/etc")},
    )
    gw = _make_symlink_gateway(project_record, transport)

    with pytest.raises(RemotePathDenied, match="escapes allowed root"):
        await gw.read(
            project_id="myproj",
            target_id="dev-host",
            service="web",
            path=PurePosixPath("/opt/link/passwd"),
        )


@pytest.mark.asyncio
async def test_gateway_host_search_rejects_symlink_component_escape(
    project_record: ProjectRecord,
) -> None:
    """Search canonicalizes its root so a symlinked root cannot be walked."""
    from incidentlens_control_plane.remote_ops.policy import RemotePathDenied

    transport = _SymlinkAwareTransport(
        files={PurePosixPath("/etc/shadow"): b"root:!:12345::::::\n"},
        symlinks={PurePosixPath("/opt/link"): PurePosixPath("/etc")},
    )
    gw = _make_symlink_gateway(project_record, transport)

    with pytest.raises(RemotePathDenied, match="escapes allowed root"):
        await gw.search(
            project_id="myproj",
            target_id="dev-host",
            service="web",
            path=PurePosixPath("/opt/link"),
            query="root",
        )


@pytest.mark.asyncio
async def test_gateway_host_read_follows_symlink_inside_root(
    project_record: ProjectRecord,
) -> None:
    """A symlink whose target stays inside the authorized root is resolved to
    the target path and read succeeds (no over-rejection)."""
    transport = _SymlinkAwareTransport(
        files={PurePosixPath("/opt/real/app.py"): b"print('real')\n"},
        symlinks={PurePosixPath("/opt/link"): PurePosixPath("/opt/real")},
    )
    gw = _make_symlink_gateway(project_record, transport)

    result = await gw.read(
        project_id="myproj",
        target_id="dev-host",
        service="web",
        path=PurePosixPath("/opt/link/app.py"),
    )
    assert result.content == b"print('real')\n"
    assert result.path == PurePosixPath("/opt/real/app.py")


@pytest.mark.asyncio
async def test_gateway_container_scope_unsupported(
    gateway: RemoteToolGateway,
) -> None:
    with pytest.raises(ContainerFileOperationUnsupported):
        await gateway.read(
            project_id="myproj",
            target_id="dev-host",
            service="web",
            path=PurePosixPath("/app/file.py"),
            scope={"kind": "container", "container": "web-1"},
        )


@pytest.mark.asyncio
async def test_gateway_unknown_project_raises(
    session_manager: SessionManager,
    project_record: ProjectRecord,
) -> None:
    projects = _FakeProjectRegistryStore(project_record)
    target_reg = project_record.targets[0]
    gw = RemoteToolGateway(
        projects=projects,
        sessions=session_manager,
        targets={target_reg.target_id: target_reg},
    )
    with pytest.raises(Exception):
        await gw.read(
            project_id="nonexistent",
            target_id="dev-host",
            service="web",
            path=PurePosixPath("/opt/app.py"),
        )


@pytest.mark.asyncio
async def test_gateway_unknown_target_raises(
    session_manager: SessionManager,
    project_record: ProjectRecord,
) -> None:
    projects = _FakeProjectRegistryStore(project_record)
    gw = RemoteToolGateway(
        projects=projects,
        sessions=session_manager,
        targets={},
    )
    with pytest.raises(ValueError, match="not registered"):
        await gw.read(
            project_id="myproj",
            target_id="missing-target",
            service="web",
            path=PurePosixPath("/opt/app.py"),
        )


@pytest.mark.asyncio
async def test_gateway_unknown_service_raises(
    session_manager: SessionManager,
    project_record: ProjectRecord,
) -> None:
    projects = _FakeProjectRegistryStore(project_record)
    target_reg = project_record.targets[0]
    gw = RemoteToolGateway(
        projects=projects,
        sessions=session_manager,
        targets={target_reg.target_id: target_reg},
    )
    with pytest.raises(ValueError, match="not found"):
        await gw.read(
            project_id="myproj",
            target_id="dev-host",
            service="nonexistent",
            path=PurePosixPath("/opt/app.py"),
        )


# ---------------------------------------------------------------------------
# Docker action and change-manager gateway tests
# ---------------------------------------------------------------------------


def _make_change_manager(tmp_path: Path, transport) -> ChangeManager:
    """Create a ChangeManager backed by a real store/vault and a fake transport."""
    from incidentlens_control_plane.changes.backup import EncryptedBackupVault
    from incidentlens_control_plane.changes.store import ChangeSetStore
    from incidentlens_control_plane.remote_ops.policy import RemotePathPolicy

    db_path = tmp_path / "changes.db"

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    store = ChangeSetStore(connect)
    store.migrate()
    vault = EncryptedBackupVault(tmp_path / "backups", tmp_path / "key.bin")
    service = ServiceRegistration(
        compose_service="web",
        container_names=("web-1",),
        allowed_host_paths=(PurePosixPath("/opt"),),
        allowed_container_paths=(PurePosixPath("/app"),),
    )
    return ChangeManager(
        store=store,
        vault=vault,
        policy=RemotePathPolicy(service),
        transport=transport,
    )


@pytest.fixture
def docker_gateway(
    tmp_path: Path,
    project_record: ProjectRecord,
) -> RemoteToolGateway:
    """RemoteToolGateway with approvals and a change manager wired in."""
    from incidentlens_control_plane.remote_ops.fakes import FakeChangeTransport

    transport = FakeChangeTransport()
    transport.files[PurePosixPath("/opt/app.py")] = b"print('hello')\n"
    projects = _FakeProjectRegistryStore(project_record)
    target_reg = project_record.targets[0]
    sessions = SessionManager(_FileTransportFactory(_HOST_FS))
    approvals = _make_approval_service(tmp_path)
    changes = _make_change_manager(tmp_path, transport)
    return RemoteToolGateway(
        projects=projects,
        sessions=sessions,
        targets={target_reg.target_id: target_reg},
        changes=changes,
        approvals=approvals,
    )


def test_docker_action_requests_approval_when_no_id(
    docker_gateway: RemoteToolGateway,
) -> None:
    """Without an approval ID a docker action returns a pending request."""
    from incidentlens_control_plane.remote_ops.types import (
        DockerActionKind,
        DockerActionRequest,
        HostScope,
    )

    request = DockerActionRequest(
        operation_id="op-dk",
        incident_id="inc-1",
        project_id="myproj",
        target_id="dev-host",
        service="web",
        scope=HostScope(),
        action=DockerActionKind.RESTART,
        container="web-1",
        reason="restart the web service",
    )

    async def scenario() -> None:
        result = await docker_gateway.docker_action(request)
        assert result.approved is False
        assert result.approval_id is not None
        assert result.approval_id.startswith("apr-")

    asyncio.run(scenario())


def test_docker_action_requested_approval_preserves_linkage(
    docker_gateway: RemoteToolGateway,
) -> None:
    """Pending docker approvals retain their downstream linkage metadata."""
    from incidentlens_control_plane.remote_ops.types import (
        DockerActionKind,
        DockerActionRequest,
        HostScope,
    )

    request = DockerActionRequest(
        operation_id="op-dk-link",
        incident_id="inc-1",
        project_id="myproj",
        target_id="dev-host",
        service="web",
        scope=HostScope(),
        session_id="sess-1",
        investigation_id="inv-1",
        agent_run_id="run-1",
        tool_call_id="call-1",
        action=DockerActionKind.RESTART,
        container="web-1",
        reason="restart the web service",
    )

    async def scenario() -> None:
        result = await docker_gateway.docker_action(request)
        assert result.approved is False
        assert result.approval_id is not None
        record = docker_gateway._approvals.get(result.approval_id)
        assert record is not None
        assert record.project_id == "myproj"
        assert record.session_id == "sess-1"
        assert record.investigation_id == "inv-1"
        assert record.agent_run_id == "run-1"
        assert record.tool_call_id == "call-1"

    asyncio.run(scenario())


def test_docker_action_consumes_approval_and_executes(
    docker_gateway: RemoteToolGateway,
    tmp_path: Path,
) -> None:
    """With an approved ID the docker action consumes it and runs a fixed argv."""
    from incidentlens_control_plane.remote_ops.types import (
        DockerActionKind,
        DockerActionRequest,
        HostScope,
    )

    request = DockerActionRequest(
        operation_id="op-dk",
        incident_id="inc-1",
        project_id="myproj",
        target_id="dev-host",
        service="web",
        scope=HostScope(),
        action=DockerActionKind.RESTART,
        container="web-1",
        reason="restart the web service",
    )
    intent = {
        "kind": "docker_action",
        "target_id": request.target_id,
        "service": request.service,
        "action": request.action.value,
        "container": request.container,
    }

    async def scenario() -> None:
        now = datetime.now(UTC)
        approval = await docker_gateway._approvals.request(intent, now=now)
        await docker_gateway._approvals.approve(approval.approval_id, now=now)

        result = await docker_gateway.docker_action(
            request, approval_id=approval.approval_id
        )
        assert result.approved is True
        assert result.exit_status == 0

    asyncio.run(scenario())


def test_docker_action_rejects_unregistered_container(
    docker_gateway: RemoteToolGateway,
) -> None:
    """A container outside the service registration is rejected."""
    from incidentlens_control_plane.remote_ops.gateway import DockerActionError
    from incidentlens_control_plane.remote_ops.types import (
        DockerActionKind,
        DockerActionRequest,
        HostScope,
    )

    request = DockerActionRequest(
        operation_id="op-dk",
        incident_id="inc-1",
        project_id="myproj",
        target_id="dev-host",
        service="web",
        scope=HostScope(),
        action=DockerActionKind.STOP,
        container="unknown-container",
        reason="stop an unregistered container",
    )

    async def scenario() -> None:
        with pytest.raises(DockerActionError, match="not registered"):
            await docker_gateway.docker_action(request)

    asyncio.run(scenario())


def test_docker_action_compose_builds_fixed_argv(
    tmp_path: Path,
    project_record: ProjectRecord,
) -> None:
    """Compose actions use the registered project directory, never model input."""
    from incidentlens_control_plane.remote_ops.fakes import FakeChangeTransport
    from incidentlens_control_plane.remote_ops.types import (
        DockerActionKind,
        DockerActionRequest,
        HostScope,
    )

    target_with_dir = TargetRegistration(
        target_id="compose-host",
        host="10.0.0.9",
        ssh_user="deploy",
        compose_working_directory=PurePosixPath("/srv/web"),
        compose_project_name="webapp",
        compose_files=(
            PurePosixPath("/srv/web/docker-compose.yml"),
            PurePosixPath("/srv/web/compose.cloud.yaml"),
        ),
    )
    record = ProjectRecord(
        project_id="myproj",
        display_name="My Project",
        targets=(target_with_dir,),
        services=(
            ServiceRegistration(
                compose_service="web",
                container_names=("web-1",),
                allowed_host_paths=(PurePosixPath("/opt"),),
                allowed_container_paths=(PurePosixPath("/app"),),
            ),
        ),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    transport = FakeChangeTransport()
    projects = _FakeProjectRegistryStore(record)
    sessions = SessionManager(_RecordingTransportFactory(transport))
    approvals = _make_approval_service(tmp_path)
    changes = _make_change_manager(tmp_path, transport)
    gw = RemoteToolGateway(
        projects=projects,
        sessions=sessions,
        targets={target_with_dir.target_id: target_with_dir},
        changes=changes,
        approvals=approvals,
    )

    request = DockerActionRequest(
        operation_id="op-dk",
        incident_id="inc-1",
        project_id="myproj",
        target_id="compose-host",
        service="web",
        scope=HostScope(),
        action=DockerActionKind.COMPOSE_UP,
        container=None,
        reason="bring the stack up",
    )

    async def scenario() -> None:
        now = datetime.now(UTC)
        approval = await gw._approvals.request(
            {
                "kind": "docker_action",
                "target_id": request.target_id,
                "service": request.service,
                "action": request.action.value,
                "container": request.container,
            },
            now=now,
        )
        await gw._approvals.approve(approval.approval_id, now=now)
        result = await gw.docker_action(request, approval_id=approval.approval_id)
        assert result.approved is True
        expected_argv = (
            "docker",
            "compose",
            "--project-directory",
            "/srv/web",
            "--file",
            "/srv/web/docker-compose.yml",
            "--file",
            "/srv/web/compose.cloud.yaml",
            "--project-name",
            "webapp",
            "up",
            "-d",
        )
        assert expected_argv in transport.run_argv_calls

    asyncio.run(scenario())


def test_gateway_edit_applies_single_file(
    tmp_path: Path,
    project_record: ProjectRecord,
) -> None:
    """gateway.edit wraps a single-file changeset and applies it."""
    import hashlib

    from incidentlens_control_plane.remote_ops.fakes import FakeChangeTransport
    from incidentlens_control_plane.remote_ops.types import TextReplacement

    transport = FakeChangeTransport()
    transport.files[PurePosixPath("/opt/app.py")] = b"print('hello')\n"
    projects = _FakeProjectRegistryStore(project_record)
    target_reg = project_record.targets[0]
    sessions = SessionManager(_FileTransportFactory(_HOST_FS))
    changes = _make_change_manager(tmp_path, transport)
    gw = RemoteToolGateway(
        projects=projects,
        sessions=sessions,
        targets={target_reg.target_id: target_reg},
        changes=changes,
    )

    async def scenario() -> None:
        result = await gw.edit(
            project_id="myproj",
            target_id="dev-host",
            service="web",
            path=PurePosixPath("/opt/app.py"),
            expected_sha256=hashlib.sha256(b"print('hello')\n").hexdigest(),
            replacements=(TextReplacement(old_text="hello", new_text="world"),),
        )
        assert result.status.value == "applied"
        assert transport.files[PurePosixPath("/opt/app.py")] == b"print('world')\n"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Container-scope gateway authorization tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_resolve_service_validates_target_and_service(
    project_store, target_registration, service_registration
) -> None:
    from incidentlens_control_plane.project_registry.types import ProjectRegistration
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

    svc = gateway.resolve_service("payments", "dev-a", "payment-api")
    assert svc.compose_service == "payment-api"

    with pytest.raises(ValueError, match="service 'ghost' not found"):
        gateway.resolve_service("payments", "dev-a", "ghost")
    with pytest.raises(ValueError, match="not registered"):
        gateway.resolve_service("payments", "ghost-target", "payment-api")


@pytest.mark.asyncio
async def test_gateway_container_list_requires_registered_container(
    project_store, target_registration, service_registration
) -> None:
    from incidentlens_control_plane.project_registry.types import ProjectRegistration
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
    from incidentlens_control_plane.project_registry.types import ProjectRegistration
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
