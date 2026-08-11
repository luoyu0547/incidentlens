"""Fakes for remote-ops transport testing — no network access required."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from incidentlens_control_plane.project_registry.types import TargetRegistration
from incidentlens_control_plane.remote_ops.transport import (
    CommandResult,
    FileMetadata,
    RemoteProcess,
)


@dataclass
class FakeProcess:
    """Minimal in-memory ``RemoteProcess`` for testing."""

    closed: bool = False

    async def write(self, data: bytes) -> None:  # noqa: ARG002
        pass

    async def read(self, max_bytes: int) -> bytes:  # noqa: ARG002
        return b""

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeTransport:
    """Minimal in-memory ``RemoteTransport`` for testing."""

    target: TargetRegistration
    alive: bool = True
    closed: bool = False
    _sftp_opened: bool = False
    _sftp_closed: bool = False
    _files: dict[PurePosixPath, bytes] = field(default_factory=dict)

    async def is_alive(self) -> bool:
        return self.alive

    async def realpath(self, path: PurePosixPath) -> PurePosixPath:
        return path

    async def lstat(self, path: PurePosixPath) -> FileMetadata:
        if path in self._files:
            return FileMetadata(
                path=path,
                size=len(self._files[path]),
                mode=0o644,
                uid=1000,
                gid=1000,
                modified_ns=0,
                is_symlink=False,
            )
        return FileMetadata(
            path=path,
            size=0,
            mode=0o644,
            uid=1000,
            gid=1000,
            modified_ns=0,
            is_symlink=False,
        )

    async def read_bytes(self, path: PurePosixPath, *, max_bytes: int) -> bytes:
        data = self._files.get(path, b"")
        return data[:max_bytes]

    async def list_directory(self, path: PurePosixPath) -> tuple[FileMetadata, ...]:
        entries: list[FileMetadata] = []
        for file_path, data in self._files.items():
            if file_path.parent == path:
                entries.append(
                    FileMetadata(
                        path=file_path,
                        size=len(data),
                        mode=0o644,
                        uid=1000,
                        gid=1000,
                        modified_ns=0,
                        is_symlink=False,
                    )
                )
        return tuple(entries)

    async def write_bytes(
        self, path: PurePosixPath, content: bytes, *, mode: int  # noqa: ARG002
    ) -> None:
        pass

    async def rename(self, source: PurePosixPath, target: PurePosixPath) -> None:  # noqa: ARG002
        pass

    async def remove_file(self, path: PurePosixPath) -> None:  # noqa: ARG002
        pass

    async def run_argv(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:  # noqa: ARG002
        return CommandResult(exit_status=0, stdout=b"", stderr=b"")

    async def open_shell(self) -> RemoteProcess:
        return FakeProcess()

    async def open_process(
        self, argv: tuple[str, ...], *, term_type: str | None  # noqa: ARG002
    ) -> RemoteProcess:
        return FakeProcess()

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeTransportFactory:
    """Tracks ``connect`` calls and created transports for assertion."""

    connect_calls: list[TargetRegistration] = field(default_factory=list)
    transports: list[FakeTransport] = field(default_factory=list)

    async def connect(self, target: TargetRegistration) -> FakeTransport:
        transport = FakeTransport(target=target)
        self.connect_calls.append(target)
        self.transports.append(transport)
        return transport


@dataclass
class FakeChangeTransport:
    """Transport that tracks all file-operation calls as human-readable strings.

    Used by ChangeManager tests to assert the exact ordering of operations.
    """

    files: dict[PurePosixPath, bytes] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    _fail_renames: set[PurePosixPath] = field(default_factory=set)
    _rename_error_msg: str = "rename failed"

    def fail_rename_for(self, path: PurePosixPath, msg: str = "rename failed") -> None:
        """Mark a path so the next rename from it raises."""
        self._fail_renames.add(path)
        self._rename_error_msg = msg

    async def is_alive(self) -> bool:
        return True

    async def realpath(self, path: PurePosixPath) -> PurePosixPath:
        self.calls.append(f"realpath:{path}")
        return path

    async def lstat(self, path: PurePosixPath) -> FileMetadata:
        self.calls.append(f"lstat:{path}")
        data = self.files.get(path, b"")
        return FileMetadata(
            path=path,
            size=len(data),
            mode=0o644,
            uid=1000,
            gid=1000,
            modified_ns=0,
            is_symlink=False,
        )

    async def read_bytes(self, path: PurePosixPath, *, max_bytes: int) -> bytes:
        self.calls.append(f"read:{path}")
        data = self.files.get(path, b"")
        return data[:max_bytes]

    async def list_directory(self, path: PurePosixPath) -> tuple[FileMetadata, ...]:
        entries: list[FileMetadata] = []
        for file_path, data in self.files.items():
            if file_path.parent == path:
                entries.append(
                    FileMetadata(
                        path=file_path,
                        size=len(data),
                        mode=0o644,
                        uid=1000,
                        gid=1000,
                        modified_ns=0,
                        is_symlink=False,
                    )
                )
        return tuple(entries)

    async def write_bytes(
        self, path: PurePosixPath, content: bytes, *, mode: int = 0o644
    ) -> None:
        self.calls.append(f"write:{path}")
        self.files[path] = content

    async def rename(self, source: PurePosixPath, target: PurePosixPath) -> None:
        if source in self._fail_renames:
            self._fail_renames.discard(source)
            from incidentlens_control_plane.remote_ops.transport import RemotePathError

            raise RemotePathError(self._rename_error_msg)
        self.calls.append(f"rename:{source}:{target}")
        if source in self.files:
            self.files[target] = self.files.pop(source)

    async def remove_file(self, path: PurePosixPath) -> None:
        self.calls.append(f"remove:{path}")
        self.files.pop(path, None)

    async def copy_file(self, source: PurePosixPath, target: PurePosixPath) -> None:
        self.calls.append(f"copy:{source}:{target}")
        if source in self.files:
            self.files[target] = self.files[source]

    async def run_argv(
        self, argv: tuple[str, ...], *, timeout: float = 30.0
    ) -> CommandResult:
        return CommandResult(exit_status=0, stdout=b"", stderr=b"")

    async def open_shell(self) -> RemoteProcess:
        return FakeProcess()

    async def open_process(
        self, argv: tuple[str, ...], *, term_type: str | None = None
    ) -> RemoteProcess:
        return FakeProcess()

    async def close(self) -> None:
        pass
