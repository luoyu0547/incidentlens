"""Tests for scoped remote file read, list, search, and stat tools."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

import pytest
from incidentlens_control_plane.project_registry.types import ServiceRegistration
from incidentlens_control_plane.remote_ops.fakes import FakeTransport
from incidentlens_control_plane.remote_ops.files import (
    FileReadResult,
    RemoteFileTools,
    SearchMatch,
)
from incidentlens_control_plane.remote_ops.policy import RemotePathDenied, RemotePathPolicy
from incidentlens_control_plane.remote_ops.transport import FileMetadata
from incidentlens_control_plane.remote_ops.types import ContainerScope, HostScope

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def service_registration() -> ServiceRegistration:
    return ServiceRegistration(
        compose_service="payment-api",
        container_names=("payments-api-1",),
        allowed_host_paths=(PurePosixPath("/opt/payments"),),
        allowed_container_paths=(PurePosixPath("/app"),),
    )


_HOST_FS: dict[PurePosixPath, bytes] = {
    PurePosixPath("/opt/payments/app.py"): b"print('ok')\n",
    PurePosixPath("/opt/payments/lib.py"): b"# lib\nx = 1\n",
}


@pytest.fixture
def host_transport() -> FakeTransport:
    return FakeTransport(
        target=None,  # type: ignore[arg-type]
        _files=_HOST_FS,
    )


# ---------------------------------------------------------------------------
# Step 1: Path-boundary tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_path_inside_registered_root_is_allowed(
    service_registration: ServiceRegistration,
) -> None:
    policy = RemotePathPolicy(service_registration)
    assert await policy.authorize(
        HostScope(), PurePosixPath("/opt/payments/app.py"), write=False
    ) == PurePosixPath("/opt/payments/app.py")


@pytest.mark.asyncio
async def test_prefix_collision_and_parent_traversal_are_rejected(
    service_registration: ServiceRegistration,
) -> None:
    policy = RemotePathPolicy(service_registration)
    with pytest.raises(RemotePathDenied):
        await policy.authorize(HostScope(), PurePosixPath("/opt/payments-other/key"), write=False)
    with pytest.raises(RemotePathDenied):
        await policy.authorize(HostScope(), PurePosixPath("/opt/payments/../secret"), write=False)


@pytest.mark.asyncio
async def test_container_scope_uses_container_roots(
    service_registration: ServiceRegistration,
) -> None:
    policy = RemotePathPolicy(service_registration)
    assert await policy.authorize(
        ContainerScope(container="payments-api-1"),
        PurePosixPath("/app/service.py"),
        write=True,
    ) == PurePosixPath("/app/service.py")


# ---------------------------------------------------------------------------
# Step 2: Bounded file-tool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_returns_content_with_sha256(
    host_transport: FakeTransport,
) -> None:
    tools = RemoteFileTools(host_transport)
    result = await tools.read(
        PurePosixPath("/opt/payments/app.py"), offset=0, limit=4096
    )
    assert isinstance(result, FileReadResult)
    assert result.content == b"print('ok')\n"
    assert result.sha256 == hashlib.sha256(result.content).hexdigest()
    assert result.path == PurePosixPath("/opt/payments/app.py")
    assert result.truncated is False


@pytest.mark.asyncio
async def test_read_supports_byte_offset_and_limit(
    host_transport: FakeTransport,
) -> None:
    tools = RemoteFileTools(host_transport)
    full = await tools.read(
        PurePosixPath("/opt/payments/app.py"), offset=0, limit=4096
    )
    partial = await tools.read(
        PurePosixPath("/opt/payments/app.py"), offset=7, limit=3
    )
    assert full.content[7:10] == partial.content
    assert partial.truncated is True


@pytest.mark.asyncio
async def test_read_rejects_response_larger_than_1_mib(
    host_transport: FakeTransport,
) -> None:
    from incidentlens_control_plane.remote_ops.files import RemoteFileError

    large_content = b"x" * (1_048_576 + 1)
    host_transport._files[PurePosixPath("/opt/payments/huge.bin")] = large_content
    tools = RemoteFileTools(host_transport)
    with pytest.raises(RemoteFileError, match="exceeds 1 MiB"):
        await tools.read(
            PurePosixPath("/opt/payments/huge.bin"), offset=0, limit=2_000_000
        )


@pytest.mark.asyncio
async def test_list_returns_metadata_without_file_bodies(
    host_transport: FakeTransport,
) -> None:
    tools = RemoteFileTools(host_transport)
    result = await tools.list(PurePosixPath("/opt/payments"))
    assert isinstance(result, tuple)
    assert len(result) > 0
    for meta in result:
        assert isinstance(meta, FileMetadata)
        assert hasattr(meta, "path")
        assert hasattr(meta, "size")
        assert hasattr(meta, "is_symlink")
    names = {m.path.name for m in result}
    assert "app.py" in names
    assert "lib.py" in names


@pytest.mark.asyncio
async def test_search_returns_at_most_200_matches(
    host_transport: FakeTransport,
) -> None:
    import random

    random.seed(42)
    for i in range(300):
        line = f"match-{i}\n"
        host_transport._files[PurePosixPath(f"/opt/payments/file_{i}.txt")] = line.encode()
    tools = RemoteFileTools(host_transport)
    matches = await tools.search(PurePosixPath("/opt/payments"), "match")
    assert isinstance(matches, tuple)
    assert len(matches) <= 200
    for m in matches:
        assert isinstance(m, SearchMatch)
        assert hasattr(m, "path")
        assert hasattr(m, "line_number")
        assert hasattr(m, "text")


@pytest.mark.asyncio
async def test_search_matches_contain_path_line_text(
    host_transport: FakeTransport,
) -> None:
    tools = RemoteFileTools(host_transport)
    matches = await tools.search(PurePosixPath("/opt/payments"), "print")
    assert len(matches) >= 1
    m = matches[0]
    assert m.path == PurePosixPath("/opt/payments/app.py")
    assert m.line_number >= 1
    assert isinstance(m.text, str)
    assert "print" in m.text


@pytest.mark.asyncio
async def test_search_skips_dot_dotdot_and_nonzero_size_directories() -> None:
    """Search never recurses through ``.``/``..`` and treats directories with a
    non-zero size as directories (real SFTP servers report a directory block
    count in ``size``)."""

    class DirAwareTransport(FakeTransport):
        async def list_directory(self, path: PurePosixPath) -> tuple[FileMetadata, ...]:
            if path == PurePosixPath("/opt/payments"):
                return (
                    FileMetadata(
                        path=PurePosixPath("/opt/payments/."),
                        size=96,
                        mode=0o40755,
                        uid=1000,
                        gid=1000,
                        modified_ns=0,
                        is_symlink=False,
                    ),
                    FileMetadata(
                        path=PurePosixPath("/opt/payments/.."),
                        size=96,
                        mode=0o40755,
                        uid=1000,
                        gid=1000,
                        modified_ns=0,
                        is_symlink=False,
                    ),
                    FileMetadata(
                        path=PurePosixPath("/opt/payments/subdir"),
                        size=96,
                        mode=0o40755,
                        uid=1000,
                        gid=1000,
                        modified_ns=0,
                        is_symlink=False,
                    ),
                    FileMetadata(
                        path=PurePosixPath("/opt/payments/app.py"),
                        size=10,
                        mode=0o100644,
                        uid=1000,
                        gid=1000,
                        modified_ns=0,
                        is_symlink=False,
                    ),
                )
            if path == PurePosixPath("/opt/payments/subdir"):
                return (
                    FileMetadata(
                        path=PurePosixPath("/opt/payments/subdir/nested.py"),
                        size=9,
                        mode=0o100644,
                        uid=1000,
                        gid=1000,
                        modified_ns=0,
                        is_symlink=False,
                    ),
                )
            return ()

        async def read_bytes(self, path: PurePosixPath, *, max_bytes: int) -> bytes:
            if path.name == "app.py":
                return b"print('root')\n"[:max_bytes]
            if path.name == "nested.py":
                return b"print('nested')\n"[:max_bytes]
            return b""

    tools = RemoteFileTools(DirAwareTransport(target=None, _files={}))  # type: ignore[arg-type]
    matches = await tools.search(PurePosixPath("/opt/payments"), "print")
    assert {m.path for m in matches} == {
        PurePosixPath("/opt/payments/app.py"),
        PurePosixPath("/opt/payments/subdir/nested.py"),
    }
