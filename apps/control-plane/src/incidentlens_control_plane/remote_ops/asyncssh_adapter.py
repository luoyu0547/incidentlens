"""AsyncSSH-backed transport implementation."""

from __future__ import annotations

import shlex
import stat
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


def _modified_ns(st: asyncssh.SFTPAttrs) -> int:
    """Return the modification time as nanoseconds from an ``SFTPAttrs``."""
    seconds = int(st.mtime) if st.mtime is not None else 0
    return seconds * 1_000_000_000 + int(st.mtime_ns or 0)


def _is_symlink(st: asyncssh.SFTPAttrs) -> bool:
    """Return whether an ``SFTPAttrs`` describes a symbolic link.

    SFTP v4+ carries an explicit file ``type``; v3 embeds the file type in
    the POSIX mode returned in ``permissions``.  Check both.
    """
    if st.type == asyncssh.sftp.FILEXFER_TYPE_SYMLINK:
        return True
    if st.permissions is not None and stat.S_ISLNK(st.permissions):
        return True
    return False


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
    """Wraps an AsyncSSH client process behind ``RemoteProcess``."""

    def __init__(self, proc: asyncssh.SSHClientProcess[bytes]) -> None:
        self._proc = proc
        self._closed = False

    async def write(self, data: bytes) -> None:
        self._proc.stdin.write(data)

    async def read(self, max_bytes: int) -> bytes:
        return await self._proc.stdout.read(max_bytes)

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._proc.stdin.close()
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
        return not self._conn.is_closed()

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
                size=st.size if st.size is not None else 0,
                mode=st.permissions if st.permissions is not None else 0,
                uid=st.uid if st.uid is not None else 0,
                gid=st.gid if st.gid is not None else 0,
                modified_ns=_modified_ns(st),
                is_symlink=_is_symlink(st),
            )
        except Exception as exc:
            raise _map_error(exc) from exc

    async def read_bytes(
        self, path: PurePosixPath, *, offset: int = 0, max_bytes: int
    ) -> bytes:
        try:
            async with self._sftp.open(str(path), "rb") as f:
                await f.seek(offset)
                return await f.read(max_bytes)
        except Exception as exc:
            raise _map_error(exc) from exc

    async def list_directory(self, path: PurePosixPath) -> tuple[FileMetadata, ...]:
        try:
            entries = await self._sftp.readdir(str(path))
            result: list[FileMetadata] = []
            for entry in entries:
                st = entry.attrs
                name = entry.filename
                if isinstance(name, bytes):
                    name = name.decode("utf-8", errors="replace")
                full = path / name
                result.append(
                    FileMetadata(
                        path=full,
                        size=st.size if st.size is not None else 0,
                        mode=st.permissions if st.permissions is not None else 0,
                        uid=st.uid if st.uid is not None else 0,
                        gid=st.gid if st.gid is not None else 0,
                        modified_ns=_modified_ns(st),
                        is_symlink=_is_symlink(st),
                    )
                )
            return tuple(result)
        except Exception as exc:
            raise _map_error(exc) from exc

    async def write_bytes(
        self,
        path: PurePosixPath,
        content: bytes,
        *,
        mode: int,
        exclusive: bool = False,
    ) -> None:
        try:
            mode_flag = "xb" if exclusive else "wb"
            async with self._sftp.open(str(path), mode_flag) as f:
                await f.write(content)
            await self._sftp.chmod(str(path), mode)
        except Exception as exc:
            raise _map_error(exc) from exc

    async def rename(self, source: PurePosixPath, target: PurePosixPath) -> None:
        # ``posix_rename`` overwrites an existing target, matching the atomic
        # replace semantics the ChangeManager relies on.  Plain FXP_RENAME
        # maps to ``renameat2(RENAME_NOREPLACE)`` on OpenSSH sftp-server and
        # would reject an existing target.
        try:
            await self._sftp.posix_rename(str(source), str(target))
        except Exception as exc:
            raise _map_error(exc) from exc

    async def remove_file(self, path: PurePosixPath) -> None:
        try:
            await self._sftp.remove(str(path))
        except Exception as exc:
            raise _map_error(exc) from exc

    async def copy_file(
        self,
        source: PurePosixPath,
        target: PurePosixPath,
        *,
        preserve: bool = True,
    ) -> None:
        # ``cp -p`` is understood by both GNU and BSD cp.  Paths are always
        # absolute (validated/canonicalized), so no ``--`` end-of-options
        # marker is needed and GNU-only ``--preserve`` is avoided.
        argv = ["cp"]
        if preserve:
            argv.append("-p")
        argv.extend([str(source), str(target)])
        try:
            result = await self.run_argv(tuple(argv), timeout=30.0)
            if result.exit_status != 0:
                raise RemotePathError(
                    f"copy failed: {result.stderr.decode(errors='replace')}"
                )
        except Exception as exc:
            raise _map_error(exc) from exc

    # --- command execution ---

    async def run_argv(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:
        cmd = shlex.join(argv)
        try:
            result = await self._conn.run(
                cmd,
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
        proc = await self._conn.create_process(
            "env PS1= sh",
            encoding=None,
        )
        return _AsyncSshProcess(proc)

    async def open_process(
        self, argv: tuple[str, ...], *, term_type: str | None
    ) -> RemoteProcess:
        cmd = shlex.join(argv)
        proc = await self._conn.create_process(
            cmd,
            encoding=None,
            term_type=term_type,
        )
        return _AsyncSshProcess(proc)

    # --- lifecycle ---

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._sftp.exit()
            await self._sftp.wait_closed()
            self._conn.close()
            await self._conn.wait_closed()


class AsyncSshTransportFactory:
    """Creates ``AsyncSshTransport`` instances for target registrations."""

    def __init__(
        self,
        *,
        client_key_paths: tuple[str, ...] | None = None,
        known_hosts_path: str | None = None,
    ) -> None:
        """Optional test-only private-key and host-key injection.

        ``client_key_paths`` and ``known_hosts_path`` are intended ONLY for
        disposable test targets (see ``tests/integration/test_live_ssh_tools.py``).
        Production targets resolve credentials through ``ssh_config_alias`` or the
        user's default SSH agent/keys and verify host keys through the user's
        default known-hosts file, so both options stay at their default ``None``.
        """
        self._client_key_paths = tuple(client_key_paths) if client_key_paths else None
        self._known_hosts_path = known_hosts_path

    async def connect(self, target: TargetRegistration) -> AsyncSshTransport:
        host = target.ssh_config_alias or target.host
        connect_kwargs: dict[str, object] = {
            "username": target.ssh_user,
            "known_hosts": (),
            "keepalive_interval": _KEEPALIVE_INTERVAL,
            "keepalive_count_max": _KEEPALIVE_COUNT_MAX,
        }
        if target.port is not None:
            connect_kwargs["port"] = target.port
        if self._client_key_paths is not None:
            connect_kwargs["client_keys"] = list(self._client_key_paths)
        if self._known_hosts_path is not None:
            connect_kwargs["known_hosts"] = self._known_hosts_path
        try:
            conn = await asyncssh.connect(host, **connect_kwargs)
        except Exception as exc:
            raise _map_error(exc) from exc
        sftp = await conn.start_sftp_client()
        return AsyncSshTransport(conn, sftp)
