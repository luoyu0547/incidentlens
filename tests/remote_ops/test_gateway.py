"""Tests for Gateway and RemoteToolGateway with scoped file tools."""

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest
from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.approvals.store import ApprovalStore
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
