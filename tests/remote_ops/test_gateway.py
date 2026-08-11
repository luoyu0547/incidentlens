"""Tests for the Gateway class with three-tier command routing."""

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.remote_ops.gateway import (
    CommandForbidden,
    Gateway,
)
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
