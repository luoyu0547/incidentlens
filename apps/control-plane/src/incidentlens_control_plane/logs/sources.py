"""On-demand log sources for file and docker container logs."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    RemoteProcess,
    RemoteTransport,
)

# The transport reads from the start of the file, so a true tail cannot be
# served without loading the whole file.  We cap the read at a generous bound
# and keep only the requested number of trailing lines as RawLogLine objects;
# the raw bytes are transient and never enter the model.
_MAX_TAIL_READ_BYTES = 16 * 1024 * 1024

_DOCKER_LOG_TIMEOUT = 30.0

# ``docker logs --timestamps`` prefixes each line with an RFC 3339 timestamp
# (e.g. ``2026-08-12T10:00:00Z``).
_DOCKER_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)
_DOCKER_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_DOCKER_STREAM_READ_SIZE = 64 * 1024


async def _drain_stderr(process: RemoteProcess) -> None:
    """Drain-and-discard a process's stderr so the pipe never fills.

    Runs as a background task for the duration of a docker ``--follow`` stream.
    Stderr is deliberately never surfaced as application log content.
    """
    while True:
        chunk = await process.read_stderr(_DOCKER_STREAM_READ_SIZE)
        if not chunk:
            return


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
        transport = session.transport
        # Read the TAIL of the file, not the head: stat the size first and read
        # back from ``max(0, size - MAX_TAIL_READ_BYTES)`` so a large log's
        # on-demand query returns recent lines rather than the stale head.
        meta = await transport.lstat(path)
        size = meta.size
        offset = max(size - _MAX_TAIL_READ_BYTES, 0)
        content = await transport.read_bytes(
            path, offset=offset, max_bytes=_MAX_TAIL_READ_BYTES
        )
        lines = content.decode("utf-8", errors="replace").splitlines()
        start = max(len(lines) - request.tail_lines, 0)
        tail = lines[start:]
        now = datetime.now(timezone.utc)
        return tuple(
            RawLogLine(
                source_ref=request.source_ref,
                cursor=f"file:{path}:{offset + start + offset_index}",
                observed_at=now,
                text=text,
            )
            for offset_index, text in enumerate(tail)
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
        content = await transport.read_bytes(
            path, offset=offset, max_bytes=_MAX_TAIL_READ_BYTES
        )
        data = content
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

    async def stream(
        self,
        subscription: LogSubscription,
        target: TargetRegistration,
        cursor: LogCursor | str | None,
    ) -> AsyncIterator[RawLogLine]:
        """Stream docker container stdout/stderr lines via a fixed ``--follow`` argv.

        The cursor (``docker:time=<iso>:seq=<n>``) supplies the ``--since``
        time; when absent or malformed the stream bootstraps to one second
        before ``observed_at``.  Each emitted line carries a ``docker:time=<ts>``
        cursor whose sequence increments per line within the same timestamp, and
        the timestamp prefix is stripped from the emitted text.

        The CLI's stderr is drained-and-discarded concurrently so a ``--follow``
        process never deadlocks on a full stderr pipe buffer, and stderr is
        never surfaced as application log content.  A ``--follow`` process must
        not end cleanly: an EOF on stdout means the CLI exited, which is treated
        as ``LogSourceUnavailable("docker log stream unavailable")`` rather than
        a clean end so the manager records a failure instead of reconnecting
        forever.  The process is always closed on exit or cancellation.
        """
        since_time = self._parse_docker_cursor(cursor)
        if since_time is None:
            since_time = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).strftime(_DOCKER_TIME_FORMAT)
        transport = self._transport_factory(target)
        try:
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
        except Exception as exc:
            raise LogSourceUnavailable("docker log stream unavailable") from exc
        buffer = b""
        seq = 0
        last_valid_ts: str | None = None
        stderr_task: asyncio.Task[None] | None = None
        try:
            stderr_task = asyncio.create_task(_drain_stderr(process))
            while True:
                try:
                    chunk = await process.read(_DOCKER_STREAM_READ_SIZE)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise LogSourceUnavailable(
                        "docker log stream unavailable"
                    ) from exc
                if not chunk:
                    raise LogSourceUnavailable("docker log stream unavailable")
                buffer += chunk
                parts = buffer.split(b"\n")
                buffer = parts.pop()  # trailing partial line, if any
                for raw in parts:
                    seq += 1
                    line, last_valid_ts = self._line_from_raw(
                        subscription, raw, seq, last_valid_ts
                    )
                    yield line
        finally:
            if stderr_task is not None:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            await process.close()

    @staticmethod
    def _parse_docker_cursor(cursor: LogCursor | str | None) -> str | None:
        """Return the ``since`` timestamp from a docker cursor, or None.

        The extracted timestamp is re-validated against the docker timestamp
        regex so a legacy ``docker:time=unknown`` cursor (or any other
        unparseable value) returns None and the caller bootstraps ``now-1s``
        instead of passing an invalid ``--since`` value to docker.
        """
        if cursor is None:
            return None
        value = cursor.cursor if isinstance(cursor, LogCursor) else cursor
        prefix = "docker:time="
        if not value.startswith(prefix):
            return None
        ts = value[len(prefix) :].split(":seq=", 1)[0]
        if not _DOCKER_TS_RE.fullmatch(ts):
            return None
        return ts

    @staticmethod
    def _line_from_raw(
        subscription: LogSubscription,
        raw: bytes,
        seq: int,
        last_valid_ts: str | None,
    ) -> tuple[RawLogLine, str | None]:
        """Build a ``RawLogLine`` from one raw streamed line.

        The docker ``--timestamps`` prefix (e.g. ``2026-08-12T10:00:00Z``) is
        parsed into the cursor timestamp and stripped from the emitted text.
        A continuation line (no parseable timestamp prefix) reuses the last
        valid timestamp so its cursor stays a parseable
        ``docker:time=<ts>:seq=<n>`` identity instead of the literal
        ``unknown`` (which would break the ``--since`` cursor on the next
        reconnect).  Returns the updated ``last_valid_ts`` so the caller can
        thread it across lines.
        """
        text = raw.decode("utf-8", errors="replace")
        head, sep, rest = text.partition(" ")
        if sep and _DOCKER_TS_RE.match(head):
            ts, message = head, rest
            last_valid_ts = ts
        else:
            message = text
            if last_valid_ts is None:
                # A continuation line with no prior timestamp: use a parseable
                # epoch fallback so the cursor never reads ``unknown``.
                last_valid_ts = "1970-01-01T00:00:00Z"
            ts = last_valid_ts
        return (
            RawLogLine(
                source_ref=subscription.source_ref,
                cursor=f"docker:time={ts}:seq={seq}",
                observed_at=datetime.now(timezone.utc),
                text=message,
            ),
            last_valid_ts,
        )
