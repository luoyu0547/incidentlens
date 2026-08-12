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
    """Minimal in-memory ``RemoteProcess`` for testing.

    ``read`` returns the next chunk from ``chunks`` and ``b""`` when the chunks
    are exhausted, so stream consumers see real byte boundaries before EOF.
    """

    closed: bool = False
    chunks: tuple[bytes, ...] = ()
    _index: int = field(default=0, init=False, repr=False)

    async def write(self, data: bytes) -> None:  # noqa: ARG002
        pass

    async def read(self, max_bytes: int) -> bytes:  # noqa: ARG002
        if self._index >= len(self.chunks):
            return b""
        chunk = self.chunks[self._index]
        self._index += 1
        return chunk

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeTransport:
    """Minimal in-memory ``RemoteTransport`` for testing.

    File operations behave like a tiny filesystem: ``read_bytes`` returns the
    stored bytes, ``write_bytes`` stores them, ``rename`` moves them, and
    ``remove_file`` deletes them.  ``lstat`` is lenient and reports metadata
    for any path so existing read-only gateway tests keep working.
    """

    target: TargetRegistration
    alive: bool = True
    closed: bool = False
    _sftp_opened: bool = False
    _sftp_closed: bool = False
    _files: dict[PurePosixPath, bytes] = field(default_factory=dict)
    open_process_calls: list[tuple[tuple[str, ...], str | None]] = field(
        default_factory=list
    )
    process_chunks: list[bytes] = field(default_factory=list)

    async def is_alive(self) -> bool:
        return self.alive

    async def realpath(self, path: PurePosixPath) -> PurePosixPath:
        return path

    async def lstat(self, path: PurePosixPath) -> FileMetadata:
        data = self._files.get(path, b"")
        return FileMetadata(
            path=path,
            size=len(data),
            mode=0o644,
            uid=1000,
            gid=1000,
            modified_ns=0,
            is_symlink=False,
        )

    async def read_bytes(
        self, path: PurePosixPath, *, offset: int = 0, max_bytes: int
    ) -> bytes:
        data = self._files.get(path, b"")
        return data[offset : offset + max_bytes]

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
        self,
        path: PurePosixPath,
        content: bytes,
        *,
        mode: int = 0o644,
        exclusive: bool = False,
    ) -> None:
        if exclusive and path in self._files:
            from incidentlens_control_plane.remote_ops.transport import RemotePathError

            raise RemotePathError(f"file already exists: {path}")
        self._files[path] = content

    async def rename(self, source: PurePosixPath, target: PurePosixPath) -> None:
        if source in self._files:
            self._files[target] = self._files.pop(source)

    async def remove_file(self, path: PurePosixPath) -> None:
        self._files.pop(path, None)

    async def copy_file(
        self, source: PurePosixPath, target: PurePosixPath, *, preserve: bool = True
    ) -> None:
        if source in self._files:
            self._files[target] = self._files[source]

    async def run_argv(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:
        return CommandResult(exit_status=0, stdout=b"", stderr=b"")

    async def open_shell(self) -> RemoteProcess:
        return FakeProcess()

    async def open_process(
        self, argv: tuple[str, ...], *, term_type: str | None
    ) -> RemoteProcess:
        self.open_process_calls.append((argv, term_type))
        return FakeProcess(chunks=tuple(self.process_chunks))

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeTransportFactory:
    """Tracks ``connect`` calls and created transports for assertion.

    Connecting to a target returns the existing *live* transport for that
    target, mirroring how ``SessionManager`` keeps one host connection per
    target.  A dead transport is replaced with a fresh one.
    """

    connect_calls: list[TargetRegistration] = field(default_factory=list)
    transports: list[FakeTransport] = field(default_factory=list)
    _live: dict[str, FakeTransport] = field(default_factory=dict, repr=False)

    async def connect(self, target: TargetRegistration) -> FakeTransport:
        existing = self._live.get(target.target_id)
        if existing is not None and existing.alive:
            return existing
        transport = FakeTransport(target=target)
        self._live[target.target_id] = transport
        self.connect_calls.append(target)
        self.transports.append(transport)
        return transport


@dataclass
class FakeChangeTransport:
    """Transport that tracks all file-operation calls as human-readable strings.

    Used by ChangeManager tests to assert the exact ordering of operations.

    Semantics mirror the real SFTP adapter closely:

    - ``lstat`` raises :class:`RemotePathError` for paths not present in
      ``files`` (so new-file writes can detect an absent target).
    - ``write_bytes`` refuses to overwrite an existing path (exclusive-create
      semantics), matching the "temporary filename already exists" guarantee.
    - ``rename`` fails when the *target* path is marked via ``fail_rename_for``.
    - ``copy_file`` fails when the *source* path is marked via ``fail_copy_for``.
    """

    files: dict[PurePosixPath, bytes] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    symlinks: set[PurePosixPath] = field(default_factory=set)
    container_files: dict[PurePosixPath, bytes] = field(default_factory=dict)
    container_symlinks: set[PurePosixPath] = field(default_factory=set)
    docker_logs: dict[tuple[str, int], bytes] = field(default_factory=dict)
    run_argv_calls: list[tuple[str, ...]] = field(default_factory=list)
    open_process_calls: list[tuple[tuple[str, ...], str | None]] = field(
        default_factory=list
    )
    process_chunks: list[bytes] = field(default_factory=list)
    _fail_renames: set[PurePosixPath] = field(default_factory=set)
    _fail_copies: set[PurePosixPath] = field(default_factory=set)
    _rename_error_msg: str = "rename failed"
    _copy_error_msg: str = "copy failed"

    def fail_rename_for(self, path: PurePosixPath, msg: str = "rename failed") -> None:
        """Mark a path so a rename *to* it raises."""
        self._fail_renames.add(path)
        self._rename_error_msg = msg

    def fail_copy_for(self, path: PurePosixPath, msg: str = "copy failed") -> None:
        """Mark a path so a copy *from* it raises."""
        self._fail_copies.add(path)
        self._copy_error_msg = msg

    async def is_alive(self) -> bool:
        return True

    async def realpath(self, path: PurePosixPath) -> PurePosixPath:
        self.calls.append(f"realpath:{path}")
        return path

    async def lstat(self, path: PurePosixPath) -> FileMetadata:
        self.calls.append(f"lstat:{path}")
        if path in self.symlinks:
            return FileMetadata(
                path=path,
                size=0,
                mode=0o120777,
                uid=1000,
                gid=1000,
                modified_ns=0,
                is_symlink=True,
            )
        if path not in self.files:
            from incidentlens_control_plane.remote_ops.transport import RemotePathError

            raise RemotePathError(f"path does not exist: {path}")
        data = self.files[path]
        return FileMetadata(
            path=path,
            size=len(data),
            mode=0o644,
            uid=1000,
            gid=1000,
            modified_ns=0,
            is_symlink=False,
        )

    async def read_bytes(
        self, path: PurePosixPath, *, offset: int = 0, max_bytes: int
    ) -> bytes:
        self.calls.append(f"read:{path}")
        data = self.files.get(path, b"")
        return data[offset : offset + max_bytes]

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
        self,
        path: PurePosixPath,
        content: bytes,
        *,
        mode: int = 0o644,
        exclusive: bool = False,
    ) -> None:
        if exclusive and path in self.files:
            from incidentlens_control_plane.remote_ops.transport import RemotePathError

            raise RemotePathError(f"file already exists: {path}")
        self.calls.append(f"write:{path}")
        self.files[path] = content

    async def rename(self, source: PurePosixPath, target: PurePosixPath) -> None:
        if target in self._fail_renames:
            self._fail_renames.discard(target)
            from incidentlens_control_plane.remote_ops.transport import RemotePathError

            raise RemotePathError(self._rename_error_msg)
        self.calls.append(f"rename:{source}:{target}")
        if source in self.files:
            self.files[target] = self.files.pop(source)

    async def remove_file(self, path: PurePosixPath) -> None:
        self.calls.append(f"remove:{path}")
        self.files.pop(path, None)

    async def copy_file(
        self, source: PurePosixPath, target: PurePosixPath, *, preserve: bool = True
    ) -> None:
        if source in self._fail_copies:
            from incidentlens_control_plane.remote_ops.transport import RemotePathError

            raise RemotePathError(self._copy_error_msg)
        self.calls.append(f"copy:{source}:{target}")
        if source in self.files:
            self.files[target] = self.files[source]

    async def run_argv(
        self, argv: tuple[str, ...], *, timeout: float = 30.0
    ) -> CommandResult:
        self.run_argv_calls.append(argv)
        simulated = self._simulate_docker_argv(argv)
        if simulated is not None:
            return simulated
        return CommandResult(exit_status=0, stdout=b"", stderr=b"")

    def _simulate_docker_argv(
        self, argv: tuple[str, ...]
    ) -> CommandResult | None:
        """Simulate fixed container file-operation argv templates."""
        if (
            len(argv) >= 7
            and argv[0] == "docker"
            and argv[1] == "logs"
            and argv[2] == "--timestamps"
            and argv[3] == "--tail"
            and argv[5] == "--"
        ):
            container = argv[6]
            return CommandResult(
                exit_status=0,
                stdout=self.docker_logs.get((container, int(argv[4])), b""),
                stderr=b"",
            )
        if len(argv) >= 4 and argv[0] == "docker" and argv[1] == "exec":
            container = argv[2]
            cmd = argv[3]
            if cmd == "cat" and "--" in argv:
                path = PurePosixPath(argv[argv.index("--") + 1])
                return CommandResult(
                    exit_status=0,
                    stdout=self.container_files.get(path, b""),
                    stderr=b"",
                )
            if cmd == "stat" and "-c" in argv and "--" in argv:
                path = PurePosixPath(argv[argv.index("--") + 1])
                data = self.container_files.get(path, b"")
                stdout = f"regular file|{len(data)}|644|1000|1000|0".encode()
                return CommandResult(exit_status=0, stdout=stdout, stderr=b"")
            if (
                cmd == "find"
                and len(argv) >= 8
                and argv[5] == "-maxdepth"
                and argv[7] == "-mindepth"
            ):
                return self._container_find_result(PurePosixPath(argv[4]))
            if cmd == "cp" and "--" in argv:
                args = [PurePosixPath(p) for p in argv[argv.index("--") + 1 :]]
                if len(args) == 2 and args[0] in self.container_files:
                    self.container_files[args[1]] = self.container_files[args[0]]
                return CommandResult(exit_status=0, stdout=b"", stderr=b"")
            if cmd == "mv" and "--" in argv:
                args = [PurePosixPath(p) for p in argv[argv.index("--") + 1 :]]
                if len(args) == 2 and args[0] in self.container_files:
                    self.container_files[args[1]] = self.container_files.pop(args[0])
                return CommandResult(exit_status=0, stdout=b"", stderr=b"")
            if cmd == "rm" and "--" in argv:
                self.container_files.pop(
                    PurePosixPath(argv[argv.index("--") + 1]), None
                )
                return CommandResult(exit_status=0, stdout=b"", stderr=b"")
            if cmd == "chmod":
                return CommandResult(exit_status=0, stdout=b"", stderr=b"")
        if (
            len(argv) >= 3
            and argv[0] == "docker"
            and argv[1] == "cp"
        ):
            host_temp = PurePosixPath(argv[2])
            container_dst = argv[3]
            if ":" in container_dst:
                container, _, dst = container_dst.partition(":")
                if host_temp in self.files:
                    self.container_files[PurePosixPath(dst)] = self.files.pop(host_temp)
                return CommandResult(exit_status=0, stdout=b"", stderr=b"")
        return None

    def _container_find_result(self, root: PurePosixPath) -> CommandResult:
        """Simulate ``find <root> -maxdepth 1 -mindepth 1 -printf ...``."""
        lines: list[str] = []
        paths = sorted(set(self.container_files) | self.container_symlinks)
        for path in paths:
            if path.parent != root:
                continue
            if path in self.container_symlinks:
                lines.append(f"{path}|l|0|777|1000|1000|0")
            else:
                lines.append(f"{path}|f|{len(self.container_files[path])}|644|1000|1000|0")
        return CommandResult(
            exit_status=0,
            stdout=("\n".join(lines) + "\n").encode(),
            stderr=b"",
        )

    async def open_shell(self) -> RemoteProcess:
        return FakeProcess()

    async def open_process(
        self, argv: tuple[str, ...], *, term_type: str | None = None
    ) -> RemoteProcess:
        self.open_process_calls.append((argv, term_type))
        return FakeProcess(chunks=tuple(self.process_chunks))

    async def close(self) -> None:
        pass
