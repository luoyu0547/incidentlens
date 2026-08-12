"""Provider-neutral transport protocols and data types.

These contracts deliberately contain no credential fields.  Adapters
resolve credentials from a secret manager at execution time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_status: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class FileMetadata:
    path: PurePosixPath
    size: int
    mode: int
    uid: int
    gid: int
    modified_ns: int
    is_symlink: bool


class RemoteProcess(Protocol):
    async def write(self, data: bytes) -> None:
        raise NotImplementedError

    async def read(self, max_bytes: int) -> bytes:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class RemoteTransport(Protocol):
    async def is_alive(self) -> bool:
        raise NotImplementedError

    async def realpath(self, path: PurePosixPath) -> PurePosixPath:
        raise NotImplementedError

    async def lstat(self, path: PurePosixPath) -> FileMetadata:
        raise NotImplementedError

    async def read_bytes(
        self, path: PurePosixPath, *, offset: int = 0, max_bytes: int
    ) -> bytes:
        raise NotImplementedError

    async def list_directory(self, path: PurePosixPath) -> tuple[FileMetadata, ...]:
        raise NotImplementedError

    async def write_bytes(
        self,
        path: PurePosixPath,
        content: bytes,
        *,
        mode: int,
        exclusive: bool = False,
    ) -> None:
        raise NotImplementedError

    async def rename(self, source: PurePosixPath, target: PurePosixPath) -> None:
        raise NotImplementedError

    async def remove_file(self, path: PurePosixPath) -> None:
        raise NotImplementedError

    async def copy_file(
        self,
        source: PurePosixPath,
        target: PurePosixPath,
        *,
        preserve: bool = True,
    ) -> None:
        raise NotImplementedError

    async def run_argv(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:
        raise NotImplementedError

    async def open_shell(self) -> RemoteProcess:
        raise NotImplementedError

    async def open_process(
        self, argv: tuple[str, ...], *, term_type: str | None
    ) -> RemoteProcess:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


@runtime_checkable
class RemoteTransportFactory(Protocol):
    async def connect(self, target: TargetRegistration) -> RemoteTransport:
        raise NotImplementedError


# --- Exceptions ---


class RemoteError(Exception):
    """Base for all remote-operation errors."""


class RemoteConnectionError(RemoteError):
    """Raised when an SSH or transport connection fails."""


class RemoteTimeoutError(RemoteError):
    """Raised when a remote command exceeds its timeout."""


class RemotePathError(RemoteError):
    """Raised when a remote path does not exist or is inaccessible."""


# Late import to avoid circular dependency at module load time.
from incidentlens_control_plane.project_registry.types import TargetRegistration  # noqa: E402
