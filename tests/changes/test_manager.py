"""Tests for ChangeManager apply, verify, and rollback."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import pytest
from incidentlens_control_plane.changes.backup import EncryptedBackupVault
from incidentlens_control_plane.changes.manager import ChangeManager
from incidentlens_control_plane.changes.store import ChangeSetStore
from incidentlens_control_plane.changes.types import ChangeSetStatus
from incidentlens_control_plane.project_registry.types import ServiceRegistration
from incidentlens_control_plane.remote_ops.fakes import FakeChangeTransport
from incidentlens_control_plane.remote_ops.policy import RemotePathPolicy
from incidentlens_control_plane.remote_ops.types import (
    ChangeSetRequest,
    FileEditRequest,
    HostScope,
    TextReplacement,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def edit(path: str, original: bytes, old: str, new: str) -> FileEditRequest:
    return FileEditRequest(
        operation_id=f"op-{PurePosixPath(path).name}",
        incident_id="inc-1",
        project_id="payments",
        target_id="dev-a",
        service="payment-api",
        scope=HostScope(),
        path=PurePosixPath(path),
        expected_sha256=hashlib.sha256(original).hexdigest(),
        replacements=(TextReplacement(old_text=old, new_text=new),),
    )


def changeset(*files: FileEditRequest) -> ChangeSetRequest:
    return ChangeSetRequest(
        changeset_id="chg-1",
        files=files,
        verification_plan="run syntax checks and compare service behavior",
        rollback_plan="restore both verified timestamped backups",
    )


def single_file_changeset(path: str) -> ChangeSetRequest:
    return changeset(edit(path, b"old\n", "old", "new"))


def two_file_edit_request() -> ChangeSetRequest:
    return changeset(
        edit("/opt/payments/a.py", b"old-a\n", "old-a", "new-a"),
        edit("/opt/payments/b.py", b"old-b\n", "old-b", "new-b"),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_transport() -> FakeChangeTransport:
    t = FakeChangeTransport()
    # Pre-populate files with original content
    t.files[PurePosixPath("/opt/payments/app.py")] = b"old\n"
    t.files[PurePosixPath("/opt/payments/a.py")] = b"old-a\n"
    t.files[PurePosixPath("/opt/payments/b.py")] = b"old-b\n"
    return t


@pytest.fixture()
def change_manager(fake_transport: FakeChangeTransport, tmp_path: Path) -> ChangeManager:
    """Create a ChangeManager with fake transport and in-memory stores."""
    db_path = tmp_path / "changes.db"

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    store = ChangeSetStore(connect)
    store.migrate()

    vault = EncryptedBackupVault(tmp_path / "backups", tmp_path / "key.bin")

    service = ServiceRegistration(
        compose_service="payment-api",
        container_names=("payments-api-1",),
        allowed_host_paths=(PurePosixPath("/opt/payments"),),
        allowed_container_paths=(PurePosixPath("/app"),),
    )
    policy = RemotePathPolicy(service)

    return ChangeManager(
        store=store,
        vault=vault,
        policy=policy,
        transport=fake_transport,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_edit_backs_up_locally_and_remotely_before_replace(
    change_manager: ChangeManager, fake_transport: FakeChangeTransport
) -> None:
    """Verify the exact two-backup ordering before any write."""
    with patch("incidentlens_control_plane.changes.manager.datetime") as mock_dt:
        from datetime import UTC, datetime

        mock_dt.now.return_value = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        result = asyncio.run(
            change_manager.apply(single_file_changeset("/opt/payments/app.py"))
        )

    assert fake_transport.calls == [
        "realpath:/opt/payments/app.py",
        "lstat:/opt/payments/app.py",
        "read:/opt/payments/app.py",
        "lstat:/opt/payments/app.py",
        "copy:/opt/payments/app.py:/opt/payments/app.py.incidentlens-backup.20260810T120000.000000Z",
        "read:/opt/payments/app.py.incidentlens-backup.20260810T120000.000000Z",
        "write:/opt/payments/.app.py.incidentlens-tmp-chg-chg-1",
        "read:/opt/payments/app.py",
        "rename:/opt/payments/.app.py.incidentlens-tmp-chg-chg-1:/opt/payments/app.py",
    ]
    assert result.status is ChangeSetStatus.APPLIED


def test_second_rename_failure_restores_first_file(
    change_manager: ChangeManager, fake_transport: FakeChangeTransport
) -> None:
    """When the second rename fails, the first file is restored from backup."""
    fake_transport.fail_rename_for(PurePosixPath("/opt/payments/b.py"))

    result = asyncio.run(change_manager.apply(two_file_edit_request()))

    assert result.status is ChangeSetStatus.ROLLED_BACK
    assert fake_transport.files[PurePosixPath("/opt/payments/a.py")] == b"old-a\n"
    assert fake_transport.files[PurePosixPath("/opt/payments/b.py")] == b"old-b\n"
    assert all(
        "rm -r" not in call and "rm -fR" not in call for call in fake_transport.calls
    )
