"""On-demand log sources for file and docker container logs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

from incidentlens_control_plane.logs.types import (
    LogCursor,
    LogQueryRequest,
    LogSubscription,
    RawLogLine,
)
from incidentlens_control_plane.project_registry.types import TargetRegistration
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.transport import (
    FileMetadata,
    RemoteTransport,
)

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


def _parse_file_offset(cursor: LogCursor) -> int:
    """Return the byte offset encoded in a ``file:offset=<int>`` cursor."""
    try:
        return int(cursor.cursor.split("=", 1)[1])
    except (IndexError, ValueError):
        return 0


def _generation_for(meta: FileMetadata) -> str:
    """Return the ``mtime=<modified_ns>:size=<size>`` generation string."""
    return f"mtime={meta.modified_ns}:size={meta.size}"


@dataclass(frozen=True, slots=True)
class FileStreamResult:
    """One file poll: the generation string, complete lines, and a rotation flag."""

    generation: str
    lines: tuple[RawLogLine, ...]
    rotated: bool = False


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

    async def stream(
        self,
        subscription: LogSubscription,
        target: TargetRegistration,
        path: PurePosixPath,
        cursor: LogCursor | None,
    ) -> FileStreamResult:
        """Poll *path* once and return the lines appended since *cursor*.

        One invocation stats the file via SFTP ``lstat``, detects rotation
        (``size < offset`` resets the offset to 0 and reports ``rotated``),
        reads the new bytes, and splits them into complete lines.  A trailing
        partial line (no trailing newline yet) is held back so it is re-read
        on the next poll.  Cursor format is ``file:offset=<int>`` and the
        generation string is ``mtime=<modified_ns>:size=<size>``.
        """
        session = await self._sessions.connect(target)
        transport = session.transport
        meta = await transport.lstat(path)
        size = meta.size
        offset = _parse_file_offset(cursor) if cursor is not None else 0
        rotated = size < offset
        if rotated:
            offset = 0
        if size <= offset:
            return FileStreamResult(generation=_generation_for(meta), lines=())
        content = await transport.read_bytes(path, max_bytes=_MAX_TAIL_READ_BYTES)
        data = content[offset:]
        last_newline = data.rfind(b"\n")
        if last_newline == -1:
            # No complete line yet; keep the offset for the next poll.
            return FileStreamResult(generation=_generation_for(meta), lines=())
        complete = data[: last_newline + 1]
        now = datetime.now(timezone.utc)
        lines: list[RawLogLine] = []
        cursor_offset = offset
        for raw in complete.split(b"\n"):
            if raw == b"":
                continue
            cursor_offset += len(raw) + 1
            lines.append(
                RawLogLine(
                    source_ref=subscription.source_ref,
                    cursor=f"file:offset={cursor_offset}",
                    observed_at=now,
                    text=raw.decode("utf-8", errors="replace"),
                )
            )
        return FileStreamResult(
            generation=_generation_for(meta), lines=tuple(lines), rotated=rotated
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
