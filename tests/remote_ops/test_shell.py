"""Tests for persistent shell framing and execution."""

from __future__ import annotations

import asyncio

import pytest
from incidentlens_control_plane.project_registry.types import ServiceRegistration
from incidentlens_control_plane.remote_ops.policy import CommandPolicy
from incidentlens_control_plane.remote_ops.shell import PersistentShell
from incidentlens_control_plane.remote_ops.types import (
    HostScope,
    OperationRisk,
    ShellRequest,
)


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


# ---------------------------------------------------------------------------
# Fake PTY process for testing PersistentShell
# ---------------------------------------------------------------------------


class FakePTYProcess:
    """Simulates a PTY byte-stream process for testing."""

    def __init__(self) -> None:
        self.start_count = 0
        self.closed = False
        self._buffer = b""
        self._should_block = False
        self._response_fn: callable | None = None

    def set_response_fn(self, fn: callable) -> None:
        """Set a function to generate responses based on marker and command."""
        self._response_fn = fn

    async def write(self, data: bytes) -> None:
        """Simulate writing to the PTY."""
        # Track first process usage only
        if self.start_count == 0:
            self.start_count = 1
        # Extract the marker from the framed command
        marker = self._extract_marker(data)
        if marker and self._response_fn:
            # Extract the command from the framed data
            command = self._extract_command(data)
            exit_status, output = self._response_fn(marker, command)
            response = self._build_response(output, exit_status, marker)
            self._buffer += response

    def _extract_marker(self, data: bytes) -> bytes | None:
        """Extract the marker from a framed command."""
        import re

        match = re.search(rb"__INCIDENTLENS_END_([0-9a-f]+)__", data)
        if match:
            return match.group(1)
        return None

    def _extract_command(self, data: bytes) -> str:
        """Extract the actual command from the framed data."""
        lines = data.split(b"\n")
        if lines:
            # The first line should be the command
            return lines[0].decode()
        return ""

    def _build_response(self, output: str, exit_status: int, marker: bytes) -> bytes:
        """Build a framed response that mirrors a non-PTY channel (no echo).

        A real non-PTY shell does not echo the command or the framing lines;
        the stream is exactly the command output followed by the marker line::

            <command output>
            __INCIDENTLENS_END_<marker>__:<status>
        """
        return (
            output.encode()
            + b"\n"
            + f"__INCIDENTLENS_END_{marker.decode()}__:{exit_status}".encode()
            + b"\n"
        )

    async def read(self, max_bytes: int) -> bytes:
        """Read from the buffer."""
        # If blocking mode is enabled and buffer is empty, wait for data
        if self._should_block and not self._buffer:
            # This will cause a timeout in PersistentShell
            await asyncio.sleep(10)
        if not self._buffer:
            return b""
        chunk = self._buffer[:max_bytes]
        self._buffer = self._buffer[max_bytes:]
        return chunk

    async def close(self) -> None:
        """Close the process."""
        self.closed = True


class FakePTYTransport:
    """Transport that returns a FakePTYProcess."""

    def __init__(self) -> None:
        self.process = FakePTYProcess()

    async def open_shell(self) -> FakePTYProcess:
        self.process.start_count += 1
        return self.process


# ---------------------------------------------------------------------------
# PersistentShell tests
# ---------------------------------------------------------------------------


class TestPersistentShell:
    """Tests for persistent shell framing."""

    @pytest.mark.asyncio
    async def test_two_calls_reuse_one_process(self) -> None:
        """Two commands should reuse the same underlying process."""
        process = FakePTYProcess()
        shell = PersistentShell(process)

        # Set up response function
        def respond(marker: bytes, command: str) -> tuple[int, str]:
            if "hello" in command:
                return 0, "hello"
            return 0, "world"

        process.set_response_fn(respond)

        await shell.execute("echo hello", timeout=1)
        await shell.execute("echo world", timeout=1)

        assert process.start_count == 1

    @pytest.mark.asyncio
    async def test_cd_affects_next_pwd(self) -> None:
        """cd /opt/payments should affect the next pwd command."""
        process = FakePTYProcess()
        shell = PersistentShell(process)

        # Track state across commands
        state = {"dir": "/root"}

        def respond(marker: bytes, command: str) -> tuple[int, str]:
            if command.startswith("cd "):
                state["dir"] = command[3:]
                return 0, ""
            elif command.startswith("pwd"):
                return 0, state["dir"]
            return 0, ""

        process.set_response_fn(respond)

        first = await shell.execute("cd /opt/payments", timeout=1)
        second = await shell.execute("pwd", timeout=1)

        assert first.exit_status == 0
        assert second.stdout.rstrip() == b"/opt/payments"

    @pytest.mark.asyncio
    async def test_exit_status_is_parsed(self) -> None:
        """Exit status should be correctly parsed from the framing."""
        process = FakePTYProcess()
        shell = PersistentShell(process)

        def respond(marker: bytes, command: str) -> tuple[int, str]:
            return 42, ""

        process.set_response_fn(respond)
        result = await shell.execute("exit 42", timeout=1)

        assert result.exit_status == 42

    @pytest.mark.asyncio
    async def test_output_is_capped_at_2_mib(self) -> None:
        """Output should be capped at 2 MiB."""
        process = FakePTYProcess()
        shell = PersistentShell(process)

        # Queue a response larger than 2 MiB
        large_output = "x" * (2 * 1024 * 1024 + 1000)

        def respond(marker: bytes, command: str) -> tuple[int, str]:
            return 0, large_output

        process.set_response_fn(respond)
        result = await shell.execute("cat /dev/urandom", timeout=5)

        assert len(result.stdout) <= 2 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_timeout_closes_shell(self) -> None:
        """Timeout should close the shell and mark it unusable."""
        process = FakePTYProcess()
        shell = PersistentShell(process)

        # Enable blocking mode - no response will be generated
        process._should_block = True

        with pytest.raises(asyncio.TimeoutError):
            await shell.execute("sleep 999", timeout=0.1)

        # Shell should be marked unusable
        assert shell.is_closed

    @pytest.mark.asyncio
    async def test_bytes_after_timeout_not_attributed(self) -> None:
        """Bytes received after a timeout should not be attributed to later commands."""
        process = FakePTYProcess()
        shell = PersistentShell(process)

        # Enable blocking mode - no response will be generated
        process._should_block = True

        with pytest.raises(asyncio.TimeoutError):
            await shell.execute("slow command", timeout=0.1)

        # Shell should be unusable after timeout
        assert shell.is_closed

        # Second command should fail because shell is closed
        with pytest.raises(RuntimeError, match="closed"):
            await shell.execute("pwd", timeout=1)


# ---------------------------------------------------------------------------
# CommandPolicy tests
# ---------------------------------------------------------------------------


class TestCommandPolicy:
    """Tests for conservative command classification."""

    def test_recursive_force_rm_is_always_forbidden(
        self,
        shell_request: ShellRequest,
        service_registration: ServiceRegistration,
    ) -> None:
        """Recursive + force rm is always forbidden.

        Covers flags in any position, including after path arguments (I1).
        """
        commands = [
            "rm -rf /opt/app",
            "rm -fr /opt/app",
            "rm -r -f /opt/app",
            "rm --recursive --force /opt/app",
            "sudo rm -R -f /opt/app",
            "command rm -fR /opt/app",
            "rm /opt/payments/app.py -rf",
            "rm /opt/payments/app.py -fr",
            "rm /opt/app -r -f",
            "sudo rm /opt/app --force --recursive",
        ]
        for command in commands:
            decision = CommandPolicy().evaluate(
                shell_request.model_copy(update={"command": command}),
                service_registration,
            )
            assert decision.risk is OperationRisk.FORBIDDEN, (
                f"Expected FORBIDDEN for {command!r}"
            )
            assert decision.approval_can_override is False

    def test_service_or_system_mutation_requires_approval(
        self,
        shell_request: ShellRequest,
        service_registration: ServiceRegistration,
    ) -> None:
        """Service or system mutation requires approval."""
        commands = [
            "docker restart payments-api-1",
            "docker rm payments-api-1",
            "docker compose up -d payment-api",
            "docker compose down",
            "apt-get install strace",
        ]
        for command in commands:
            decision = CommandPolicy().evaluate(
                shell_request.model_copy(update={"command": command}),
                service_registration,
            )
            assert decision.risk is OperationRisk.APPROVAL_REQUIRED, (
                f"Expected APPROVAL_REQUIRED for {command!r}"
            )

    def test_pwd_is_automatic_read(
        self,
        shell_request: ShellRequest,
        service_registration: ServiceRegistration,
    ) -> None:
        """pwd should be an automatic read."""
        decision = CommandPolicy().evaluate(
            shell_request.model_copy(update={"command": "pwd"}),
            service_registration,
        )
        assert decision.risk is OperationRisk.AUTO_READ

    def test_docker_ps_is_automatic_read(
        self,
        shell_request: ShellRequest,
        service_registration: ServiceRegistration,
    ) -> None:
        """docker ps should be an automatic read."""
        decision = CommandPolicy().evaluate(
            shell_request.model_copy(update={"command": "docker ps"}),
            service_registration,
        )
        assert decision.risk is OperationRisk.AUTO_READ

    def test_ls_cat_stat_automatic_when_paths_valid(
        self,
        shell_request: ShellRequest,
        service_registration: ServiceRegistration,
    ) -> None:
        """ls, cat, and stat are automatic when path arguments are valid."""
        commands = [
            "ls /opt/payments",
            "cat /opt/payments/app.py",
            "stat /opt/payments/app.py",
        ]
        for command in commands:
            decision = CommandPolicy().evaluate(
                shell_request.model_copy(update={"command": command}),
                service_registration,
            )
            assert decision.risk is OperationRisk.AUTO_READ, (
                f"Expected AUTO_READ for {command!r}"
            )

    def test_ls_cat_stat_with_relative_parent_arg_requires_approval(
        self,
        shell_request: ShellRequest,
        service_registration: ServiceRegistration,
    ) -> None:
        """A relative ``../`` argument is not authorized (I2).

        In a persistent shell whose CWD is arbitrary, a relative argument can
        read outside the registered roots, so it must not be AUTO_READ.
        """
        commands = [
            "ls /opt/payments ../secret",
            "cat /opt/payments/app.py ../../etc/passwd",
            "stat /opt/payments ../../etc/passwd",
        ]
        for command in commands:
            decision = CommandPolicy().evaluate(
                shell_request.model_copy(update={"command": command}),
                service_registration,
            )
            assert decision.risk is OperationRisk.APPROVAL_REQUIRED, (
                f"Expected APPROVAL_REQUIRED for {command!r}"
            )

    def test_sed_i_is_rejected(
        self,
        shell_request: ShellRequest,
        service_registration: ServiceRegistration,
    ) -> None:
        """sed -i should be rejected with reason about remote_edit."""
        decision = CommandPolicy().evaluate(
            shell_request.model_copy(update={"command": "sed -i 's/foo/bar/' file.txt"}),
            service_registration,
        )
        assert decision.risk is OperationRisk.FORBIDDEN
        assert "remote_edit" in decision.reason.lower()

    def test_unclassified_commands_require_approval(
        self,
        shell_request: ShellRequest,
        service_registration: ServiceRegistration,
    ) -> None:
        """Unclassified commands should require approval."""
        commands = [
            "make build",
            "cargo test",
            "npm install",
        ]
        for command in commands:
            decision = CommandPolicy().evaluate(
                shell_request.model_copy(update={"command": command}),
                service_registration,
            )
            assert decision.risk is OperationRisk.APPROVAL_REQUIRED, (
                f"Expected APPROVAL_REQUIRED for {command!r}"
            )

    def test_command_substitution_is_forbidden(
        self,
        shell_request: ShellRequest,
        service_registration: ServiceRegistration,
    ) -> None:
        """Command substitution should be forbidden."""
        commands = [
            "echo $(cat /etc/passwd)",
            "echo `cat /etc/passwd`",
        ]
        for command in commands:
            decision = CommandPolicy().evaluate(
                shell_request.model_copy(update={"command": command}),
                service_registration,
            )
            assert decision.risk is OperationRisk.FORBIDDEN, (
                f"Expected FORBIDDEN for {command!r}"
            )

    def test_eval_is_forbidden(
        self,
        shell_request: ShellRequest,
        service_registration: ServiceRegistration,
    ) -> None:
        """eval should be forbidden."""
        decision = CommandPolicy().evaluate(
            shell_request.model_copy(update={"command": "eval dangerous_code"}),
            service_registration,
        )
        assert decision.risk is OperationRisk.FORBIDDEN

    def test_nul_byte_is_forbidden(
        self,
        shell_request: ShellRequest,
        service_registration: ServiceRegistration,
    ) -> None:
        """NUL byte in command should be forbidden."""
        decision = CommandPolicy().evaluate(
            shell_request.model_copy(update={"command": "echo\x00malicious"}),
            service_registration,
        )
        assert decision.risk is OperationRisk.FORBIDDEN

    def test_newline_in_command_is_forbidden(
        self,
        shell_request: ShellRequest,
        service_registration: ServiceRegistration,
    ) -> None:
        """Newlines used to hide additional commands should be forbidden."""
        decision = CommandPolicy().evaluate(
            shell_request.model_copy(update={"command": "echo hello\nrm -rf /"}),
            service_registration,
        )
        assert decision.risk is OperationRisk.FORBIDDEN
