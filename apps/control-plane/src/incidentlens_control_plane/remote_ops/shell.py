"""Persistent shell framing for safe remote command execution."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass

from incidentlens_control_plane.remote_ops.transport import RemoteProcess

# Maximum output size: 2 MiB
_MAX_OUTPUT_SIZE = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ShellResult:
    """Result of a shell command execution."""

    command: str
    stdout: bytes
    exit_status: int


class PersistentShell:
    """Manages a persistent PTY session with framed command execution.

    Commands are serialized with an asyncio.Lock. Each command is wrapped
    with a unique marker to cleanly separate output. The shell tracks
    exit status and enforces output limits.
    """

    def __init__(self, process: RemoteProcess) -> None:
        self._process = process
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def is_closed(self) -> bool:
        """Whether the shell has been closed or marked unusable."""
        return self._closed

    async def execute(self, command: str, *, timeout: float) -> ShellResult:
        """Execute a command in the persistent shell.

        Args:
            command: The shell command to execute.
            timeout: Maximum time in seconds to wait for the command.

        Returns:
            ShellResult with stdout and exit_status.

        Raises:
            asyncio.TimeoutError: If the command exceeds the timeout.
            RuntimeError: If the shell is closed.
        """
        if self._closed:
            raise RuntimeError("shell is closed")

        async with self._lock:
            return await self._execute_locked(command, timeout)

    async def _execute_locked(self, command: str, timeout: float) -> ShellResult:
        """Execute a command while holding the lock."""
        # Generate a unique 128-bit marker
        marker = secrets.token_hex(16)
        marker_bytes = f"__INCIDENTLENS_END_{marker}__".encode()

        # Build the framed command
        framed_command = (
            f"{command}\n"
            f"__incidentlens_status=$?\n"
            f"printf '\\n__INCIDENTLENS_END_{marker}__:%s\\n' \"$__incidentlens_status\"\n"
        )

        # Write the command
        await self._process.write(framed_command.encode())

        # Read until we see the marker
        output = b""
        try:
            output = await asyncio.wait_for(
                self._read_until_marker(marker_bytes),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Mark shell as unusable after timeout
            self._closed = True
            try:
                await self._process.close()
            except Exception:
                pass
            raise

        # Parse the output to extract stdout and exit status
        stdout, exit_status = self._parse_output(output, marker_bytes)

        # Cap output at 2 MiB
        if len(stdout) > _MAX_OUTPUT_SIZE:
            stdout = stdout[:_MAX_OUTPUT_SIZE]

        return ShellResult(
            command=command,
            stdout=stdout,
            exit_status=exit_status,
        )

    async def _read_until_marker(self, marker: bytes) -> bytes:
        """Read from the process until the marker is found."""
        buffer = b""
        while True:
            chunk = await self._process.read(4096)
            if not chunk:
                break
            buffer += chunk
            if marker in buffer:
                break
        return buffer

    def _parse_output(self, output: bytes, marker: bytes) -> tuple[bytes, int]:
        """Parse the framed output to extract stdout and exit status.

        The output format is:
        <command echo>
        <command output>
        __incidentlens_status=<status>
        <marker>:<status>
        """
        # Find the marker line
        marker_idx = output.find(marker)
        if marker_idx == -1:
            # Protocol loss - marker not found
            self._closed = True
            return output, -1

        # Extract the line after the marker
        after_marker = output[marker_idx + len(marker):]
        # Find the colon and status
        colon_idx = after_marker.find(b":")
        if colon_idx == -1:
            self._closed = True
            return output, -1

        try:
            status_str = after_marker[colon_idx + 1:after_marker.find(b"\n")].strip()
            exit_status = int(status_str)
        except (ValueError, IndexError):
            self._closed = True
            return output, -1

        # Extract stdout (everything before the framing lines)
        # Remove the echoed command and framing
        lines = output.split(b"\n")
        stdout_lines = []
        skip_next = False
        for line in lines:
            # Skip the echoed command
            if skip_next:
                skip_next = False
                continue
            # Skip framing lines
            if line.strip().startswith(b"__incidentlens_status="):
                continue
            if marker in line:
                continue
            stdout_lines.append(line)

        # Remove the first line (echoed command)
        if stdout_lines:
            stdout_lines = stdout_lines[1:]

        stdout = b"\n".join(stdout_lines)
        # Remove trailing newline if present
        if stdout.endswith(b"\n"):
            stdout = stdout[:-1]

        return stdout, exit_status

    async def close(self) -> None:
        """Close the shell."""
        if not self._closed:
            self._closed = True
            try:
                await self._process.close()
            except Exception:
                pass
