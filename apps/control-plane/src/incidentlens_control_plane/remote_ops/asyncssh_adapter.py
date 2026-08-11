"""AsyncSSH-backed transport implementation."""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

import asyncssh

from incidentlens_control_plane.project_registry.types import TargetRegistration
from incidentlens_control_plane.remote_ops.transport import (
    CommandResult,
    FileMetadata,
    RemoteConnectionError,
    RemotePathError,
    RemoteProcess,
    RemoteTimeoutError,
    RemoteTransport,
)

_KEEPALIVE_INTERVAL = 15
_KEEPALIVE_COUNT_MAX = 3


def _map_error(exc: Exception) -> Exception:
    """Map AsyncSSH library errors into domain exceptions."""
    if isinstance(exc, (asyncssh.ConnectionLost, OSError)):
        return RemoteConnectionError(str(exc))
    if isinstance(exc, asyncssh.TimeoutError):
        return RemoteTimeoutError(str(exc))
    if isinstance(exc, (asyncssh.SFTPEOFError, asyncssh.SFTPError)):
        return RemotePathError(str(exc))
    return exc


class _AsyncSshProcess:
    """Wraps an AsyncSSH process stdin/stdout behind ``RemoteProcess``."""

    def __init__(self, proc: asyncssh.SSHClientChannel) -> None:
        self._proc = proc
        self._closed = False

    async def write(self, data: bytes) -> None:
        self._proc.write(data)

    async def read(self, max_bytes: int) -> bytes:
        return self._proc.read(max_bytes)

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._proc.close()
            await self._proc.wait_closed()


class AsyncSshTransport(RemoteTransport):
    """``RemoteTransport`` backed by an ``asyncssh.SSHClientConnection``."""

    def __init__(
        self,
        conn: asyncssh.SSHClientConnection,
        sftp_client: asyncssh.SFTPClient,
    ) -> None:
        self._conn = conn
        self._sftp = sftp_client
        self._closed = False

    # --- transport liveness ---

    async def is_alive(self) -> bool:
        return not self._conn.closed

    # --- SFTP operations ---

    async def realpath(self, path: PurePosixPath) -> PurePosixPath:
        try:
            return PurePosixPath(await self._sftp.realpath(str(path)))
        except Exception as exc:
            raise _map_error(exc) from exc

    async def lstat(self, path: PurePosixPath) -> FileMetadata:
        try:
            st = await self._sftp.lstat(str(path))
            return FileMetadata(
                path=path,
                size=st.st_size,
                mode=st.st_mode,
                uid=st.st_uid,
                gid=st.st_gid,
                modified_ns=st.st_mtime_ns if hasattr(st, "st_mtime_ns") else 0,
                is_symlink=False,
            )
        except Exception as exc:
            raise _map_error(exc) from exc

    async def read_bytes(self, path: PurePosixPath, *, max_bytes: int) -> bytes:
        try:
            async with self._sftp.open(str(path), "rb") as f:
                return await f.read(max_bytes)
        except Exception as exc:
            raise _map_error(exc) from exc

    async def list_directory(self, path: PurePosixPath) -> tuple[FileMetadata, ...]:
        try:
            entries = await self._sftp.listdir_attr(str(path))
            result: list[FileMetadata] = []
            for entry in entries:
                name = entry.filename
                attrs = entry.attr
                full = path / name
                result.append(
                    FileMetadata(
                        path=full,
                        size=attrs.st_size if attrs.st_size is not None else 0,
                        mode=attrs.st_mode if attrs.st_mode is not None else 0,
                        uid=attrs.st_uid if attrs.st_uid is not None else 0,
                        gid=attrs.st_gid if attrs.st_gid is not None else 0,
                        modified_ns=(
                            attrs.st_mtime_ns
                            if hasattr(attrs, "st_mtime_ns") and attrs.st_mtime_ns is not None
                            else 0
                        ),
                        is_symlink=False,
                    )
                )
            return tuple(result)
        except Exception as exc:
            raise _map_error(exc) from exc

    async def write_bytes(
        self, path: PurePosixPath, content: bytes, *, mode: int
    ) -> None:
        try:
            async with self._sftp.open(str(path), "wb") as f:
                await f.write(content)
            await self._sftp.chmod(str(path), mode)
        except Exception as exc:
            raise _map_error(exc) from exc

    async def rename(self, source: PurePosixPath, target: PurePosixPath) -> None:
        try:
            await self._sftp.rename(str(source), str(target))
        except Exception as exc:
            raise _map_error(exc) from exc

    async def remove_file(self, path: PurePosixPath) -> None:
        try:
            await self._sftp.remove(str(path))
        except Exception as exc:
            raise _map_error(exc) from exc

    # --- command execution ---

    async def run_argv(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:
        cmd = shlex.join(argv)
        try:
            result = await asyncssh.run(
                cmd,
                stdin=None,
                stdout=asyncssh.PIPE,
                stderr=asyncssh.PIPE,
                encoding=None,
                check=False,
                timeout=timeout,
            )
            return CommandResult(
                exit_status=result.exit_status,
                stdout=result.stdout or b"",
                stderr=result.stderr or b"",
            )
        except asyncssh.TimeoutError as exc:
            raise RemoteTimeoutError(str(exc)) from exc
        except Exception as exc:
            raise _map_error(exc) from exc

    # --- interactive processes ---

    async def open_shell(self) -> RemoteProcess:
        chan, _ = await self._conn.create_session(
            asyncssh.SSHClientChannel, "env PS1= sh"
        )
        return _AsyncSshProcess(chan)

    async def open_process(
        self, argv: tuple[str, ...], *, term_type: str | None
    ) -> RemoteProcess:
        cmd = shlex.join(argv)
        chan, _ = await self._conn.create_session(
            asyncssh.SSHClientChannel, cmd, term_type=term_type
        )
        return _AsyncSshProcess(chan)

    # --- lifecycle ---

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._sftp.close()
            self._conn.close()
            await self._conn.wait_closed()


class AsyncSshTransportFactory:
    """Creates ``AsyncSshTransport`` instances for target registrations."""

    async def connect(self, target: TargetRegistration) -> AsyncSshTransport:
        host = target.ssh_config_alias or target.host
        try:
            conn = await asyncssh.connect(
                host,
                username=target.ssh_user,
                known_hosts=(),
                keepalive_interval=_KEEPALIVE_INTERVAL,
                keepalive_count_max=_KEEPALIVE_COUNT_MAX,
            )
        except Exception as exc:
            raise _map_error(exc) from exc
        sftp = await conn.start_sftp_client()
        return AsyncSshTransport(conn, sftp)
