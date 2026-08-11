"""Scoped remote file read, list, search, and stat tools.

These tools operate on an already-authorized path and delegate I/O to the
underlying :class:`~incidentlens_control_plane.remote_ops.transport.RemoteTransport`.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.remote_ops.transport import (
    FileMetadata,
    RemoteTransport,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_READ_BYTES = 1_048_576  # 1 MiB
_MAX_SEARCH_MATCHES = 200
_MAX_SEARCH_FILES = 10_000
_MAX_SEARCH_FILE_BYTES = 1_048_576  # 1 MiB per file during search


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RemoteFileError(Exception):
    """Raised when a remote file operation fails."""


class ContainerFileOperationUnsupported(RemoteFileError):
    """Raised when a container-scope file operation is requested before
    Task 7 installs the fixed Docker file backend."""


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class FileReadResult(BaseModel):
    """Result of a remote file read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: PurePosixPath
    content: bytes
    sha256: str
    metadata: FileMetadata
    truncated: bool


class SearchMatch(BaseModel):
    """A single search match inside a remote file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: PurePosixPath
    line_number: int = Field(ge=1)
    text: str = Field(max_length=2_000)


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------


class RemoteFileTools:
    """Bounded, read-only file operations over a :class:`RemoteTransport`.

    Every public method enforces scope limits defined by the class constants
    (max read size, max search matches, max files traversed).
    """

    def __init__(self, transport: RemoteTransport) -> None:
        self._transport = transport

    # --- read ---

    async def read(
        self,
        path: PurePosixPath,
        *,
        offset: int = 0,
        limit: int = _MAX_READ_BYTES,
    ) -> FileReadResult:
        """Read *limit* bytes from *path* starting at *offset*.

        Raises :class:`RemoteFileError` if the response exceeds 1 MiB.
        """
        raw = await self._transport.read_bytes(path, max_bytes=offset + limit)
        content = raw[offset:]
        if len(content) > _MAX_READ_BYTES:
            raise RemoteFileError(
                f"response size {len(content)} exceeds 1 MiB limit"
            )

        metadata = await self._transport.lstat(path)
        truncated = (offset + limit) < metadata.size

        return FileReadResult(
            path=path,
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            metadata=metadata,
            truncated=truncated,
        )

    # --- list ---

    async def list(self, path: PurePosixPath) -> tuple[FileMetadata, ...]:
        """Return directory entries as :class:`FileMetadata` (no file bodies)."""
        return await self._transport.list_directory(path)

    # --- search ---

    async def search(
        self,
        path: PurePosixPath,
        query: str,
    ) -> tuple[SearchMatch, ...]:
        """Walk *path* recursively, returning lines matching *query*.

        Constraints enforced:
        - Only files (symlinks are skipped).
        - At most 10 000 files traversed.
        - At most 1 MiB read per file.
        - At most 200 matches returned.
        """
        matches: list[SearchMatch] = []
        files_visited = 0
        dirs: list[PurePosixPath] = [path]

        while dirs and files_visited < _MAX_SEARCH_FILES:
            current = dirs.pop(0)
            entries = await self._transport.list_directory(current)

            for entry in entries:
                if len(matches) >= _MAX_SEARCH_MATCHES:
                    return tuple(matches)

                if entry.is_symlink:
                    continue

                if entry.size == 0:
                    # Likely a directory; try listing it.
                    sub_entries = await self._transport.list_directory(entry.path)
                    if sub_entries or entry.size == 0:
                        # Heuristic: if lstat says 0 bytes, treat as directory.
                        dirs.append(entry.path)
                    continue

                files_visited += 1
                data = await self._transport.read_bytes(
                    entry.path, max_bytes=_MAX_SEARCH_FILE_BYTES
                )
                for line_no, line in enumerate(data.split(b"\n"), start=1):
                    if len(matches) >= _MAX_SEARCH_MATCHES:
                        return tuple(matches)
                    try:
                        text = line.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    if query in text:
                        matches.append(
                            SearchMatch(
                                path=entry.path,
                                line_number=line_no,
                                text=text[:2_000],
                            )
                        )

        return tuple(matches)

    # --- stat ---

    async def stat(self, path: PurePosixPath) -> FileMetadata:
        """Return metadata for *path*.

        Raises :class:`RemoteFileError` if the path is a symbolic link
        (symlink support is deferred to a later phase).
        """
        meta = await self._transport.lstat(path)
        if meta.is_symlink:
            raise RemoteFileError(
                f"symbolic links are not supported: {path}"
            )
        return meta
