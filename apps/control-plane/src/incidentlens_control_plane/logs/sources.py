"""On-demand log sources for file and docker container logs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import PurePosixPath

from incidentlens_control_plane.logs.types import LogQueryRequest, RawLogLine
from incidentlens_control_plane.project_registry.types import TargetRegistration
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.transport import RemoteTransport

# The transport reads from the start of the file, so a true tail cannot be
# served without loading the whole file.  We cap the read at a generous bound
# and keep only the requested number of trailing lines as RawLogLine objects;
# the raw bytes are transient and never enter the model.
_MAX_TAIL_READ_BYTES = 16 * 1024 * 1024

_DOCKER_LOG_TIMEOUT = 30.0


class LogSourceUnavailable(Exception):
    """Raised when a remote log source cannot be read.

    The message is a fixed safe string and never includes raw remote output.
    """


class FileLogSource:
    """Read the tail of a log file on a remote host."""

    def __init__(self, sessions: SessionManager) -> None:
        self._sessions = sessions

    async def query(
        self,
        request: LogQueryRequest,
        target: TargetRegistration,
        path: PurePosixPath,
    ) -> tuple[RawLogLine, ...]:
        session = await self._sessions.connect(target)
        content = await session.transport.read_bytes(
            path, max_bytes=_MAX_TAIL_READ_BYTES
        )
        lines = content.decode("utf-8", errors="replace").splitlines()
        start = max(len(lines) - request.tail_lines, 0)
        tail = lines[start:]
        now = datetime.now(timezone.utc)
        return tuple(
            RawLogLine(
                source_ref=request.source_ref,
                cursor=f"file:{path}:{start + offset}",
                observed_at=now,
                text=text,
            )
            for offset, text in enumerate(tail)
        )


class DockerLogSource:
    """Read a bounded tail of a docker container's logs via a fixed argv."""

    def __init__(
        self, transport_factory: Callable[[TargetRegistration], RemoteTransport]
    ) -> None:
        self._transport_factory = transport_factory

    async def query(
        self, request: LogQueryRequest, target: TargetRegistration
    ) -> tuple[RawLogLine, ...]:
        transport = self._transport_factory(target)
        tail = str(min(max(request.tail_lines, 1), 1000))
        result = await transport.run_argv(
            (
                "docker",
                "logs",
                "--timestamps",
                "--tail",
                tail,
                "--",
                request.source_ref,
            ),
            timeout=_DOCKER_LOG_TIMEOUT,
        )
        if result.exit_status != 0:
            raise LogSourceUnavailable("docker logs failed")
        lines = result.stdout.decode("utf-8", errors="replace").splitlines()
        start = max(len(lines) - request.tail_lines, 0)
        tail_lines = lines[start:]
        now = datetime.now(timezone.utc)
        return tuple(
            RawLogLine(
                source_ref=request.source_ref,
                cursor=f"docker:{request.source_ref}:{start + offset}",
                observed_at=now,
                text=text,
            )
            for offset, text in enumerate(tail_lines)
        )
