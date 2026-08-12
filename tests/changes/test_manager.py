"""Tests for ChangeManager apply, verify, and rollback."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import pytest
from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.approvals.types import ApprovalStatus
from incidentlens_control_plane.changes.backup import EncryptedBackupVault
from incidentlens_control_plane.changes.manager import (
    ChangeApplyError,
    ChangeManager,
    ChangeRollbackError,
    is_protected_path,
    protected_paths_intent,
)
from incidentlens_control_plane.changes.store import ChangeSetStore
from incidentlens_control_plane.changes.types import ChangeSetStatus, FileChange
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.project_registry.types import ServiceRegistration
from incidentlens_control_plane.remote_ops.fakes import (
    FakeChangeTransport,
    FakeTransportFactory,
)
from incidentlens_control_plane.remote_ops.files import ContainerFileBackend
from incidentlens_control_plane.remote_ops.policy import RemotePathPolicy
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.types import (
    ChangeSetRequest,
    ContainerScope,
    FileEditRequest,
    FileWriteRequest,
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


def write(path: str, content: bytes) -> FileWriteRequest:
    return FileWriteRequest(
        operation_id=f"op-{PurePosixPath(path).name}",
        incident_id="inc-1",
        project_id="payments",
        target_id="dev-a",
        service="payment-api",
        scope=HostScope(),
        path=PurePosixPath(path),
        expected_sha256=None,
        content=content,
    )


def changeset(*files: FileEditRequest | FileWriteRequest) -> ChangeSetRequest:
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


def seed_applied_changeset(
    store: ChangeSetStore,
    *,
    changeset_id: str = "chg-applied",
    remote_path: str = "/opt/payments/app.py",
) -> str:
    """Persist an APPLIED changeset directly, without touching a transport."""
    store.create_changeset(
        changeset_id=changeset_id,
        incident_id="inc-1",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        files=(
            FileChange(
                file_change_id="op-1",
                scope="host",
                remote_path=remote_path,
                expected_sha256=None,
                replacement_sha256="b" * 64,
                diff_text="",
                original_metadata={},
                local_backup_ref="backup.enc",
                remote_backup_path=(
                    f"{remote_path}.incidentlens-backup.20260810T120000.000000Z"
                ),
                temp_path=None,
                applied=True,
                validation_result="validated",
                rollback_result=None,
            ),
        ),
        verification_plan="run syntax checks and compare service behavior",
        rollback_plan="restore the verified timestamped backup",
    )
    for status in (
        ChangeSetStatus.PREFLIGHTED,
        ChangeSetStatus.LOCALLY_BACKED_UP,
        ChangeSetStatus.REMOTELY_BACKED_UP,
        ChangeSetStatus.APPLIED,
    ):
        store.transition(changeset_id, status)
    return changeset_id


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
# Step 1: Two-backup ordering and failure modes
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
        "lstat:/opt/payments/app.py",
        "realpath:/opt/payments/app.py",
        "read:/opt/payments/app.py",
        "copy:/opt/payments/app.py:/opt/payments/app.py.incidentlens-backup.20260810T120000.000000Z",
        "read:/opt/payments/app.py.incidentlens-backup.20260810T120000.000000Z",
        "write:/opt/payments/.app.py.incidentlens-tmp-chg-1",
        "rename:/opt/payments/.app.py.incidentlens-tmp-chg-1:/opt/payments/app.py",
    ]
    assert result.status is ChangeSetStatus.APPLIED


def test_local_backup_failure_prevents_write(
    change_manager: ChangeManager, fake_transport: FakeChangeTransport
) -> None:
    """When the encrypted local backup fails, no remote write happens."""
    with patch.object(
        change_manager._vault,
        "store",
        side_effect=RuntimeError("vault unavailable"),
    ):
        result = asyncio.run(
            change_manager.apply(single_file_changeset("/opt/payments/app.py"))
        )

    assert result.status is ChangeSetStatus.FAILED
    assert not any(call.startswith("write:") for call in fake_transport.calls)
    assert fake_transport.files[PurePosixPath("/opt/payments/app.py")] == b"old\n"


def test_remote_cp_failure_prevents_write(
    fake_transport: FakeChangeTransport, change_manager: ChangeManager
) -> None:
    """When remote ``cp --preserve`` fails, no write happens."""
    fake_transport.fail_copy_for(PurePosixPath("/opt/payments/app.py"))

    result = asyncio.run(
        change_manager.apply(single_file_changeset("/opt/payments/app.py"))
    )

    assert result.status is ChangeSetStatus.FAILED
    assert not any(call.startswith("write:") for call in fake_transport.calls)
    assert fake_transport.files[PurePosixPath("/opt/payments/app.py")] == b"old\n"


def test_backup_hash_mismatch_prevents_write(
    change_manager: ChangeManager, fake_transport: FakeChangeTransport
) -> None:
    """When the local backup does not round-trip, no write happens."""
    real_load = change_manager._vault.load

    def tampered(ref):
        return real_load(ref) + b"tampered"

    with patch.object(change_manager._vault, "load", side_effect=tampered):
        result = asyncio.run(
            change_manager.apply(single_file_changeset("/opt/payments/app.py"))
        )

    assert result.status is ChangeSetStatus.FAILED
    assert not any(call.startswith("write:") for call in fake_transport.calls)
    assert fake_transport.files[PurePosixPath("/opt/payments/app.py")] == b"old\n"


def test_stale_source_hash_prevents_write(
    fake_transport: FakeChangeTransport, change_manager: ChangeManager
) -> None:
    """When the source hash is stale, no backup or write happens."""
    fake_transport.files[PurePosixPath("/opt/payments/app.py")] = b"changed\n"

    result = asyncio.run(
        change_manager.apply(single_file_changeset("/opt/payments/app.py"))
    )

    assert result.status is ChangeSetStatus.FAILED
    assert not any(call.startswith("write:") for call in fake_transport.calls)
    assert not any(call.startswith("copy:") for call in fake_transport.calls)


def test_symlink_target_prevents_write(
    fake_transport: FakeChangeTransport, change_manager: ChangeManager
) -> None:
    """A symlink target is rejected before any backup or write."""
    fake_transport.symlinks.add(PurePosixPath("/opt/payments/app.py"))

    result = asyncio.run(
        change_manager.apply(single_file_changeset("/opt/payments/app.py"))
    )

    assert result.status is ChangeSetStatus.FAILED
    assert not any(call.startswith("write:") for call in fake_transport.calls)
    assert fake_transport.files[PurePosixPath("/opt/payments/app.py")] == b"old\n"


def test_existing_temp_filename_prevents_write(
    fake_transport: FakeChangeTransport, change_manager: ChangeManager
) -> None:
    """A pre-existing temp filename is never silently overwritten."""
    fake_transport.files[
        PurePosixPath("/opt/payments/.app.py.incidentlens-tmp-chg-1")
    ] = b"attacker-controlled"

    result = asyncio.run(
        change_manager.apply(single_file_changeset("/opt/payments/app.py"))
    )

    assert result.status is ChangeSetStatus.FAILED
    assert fake_transport.files[PurePosixPath("/opt/payments/app.py")] == b"old\n"
    assert (
        fake_transport.files[
            PurePosixPath("/opt/payments/.app.py.incidentlens-tmp-chg-1")
        ]
        == b"attacker-controlled"
    )


# ---------------------------------------------------------------------------
# Step 2: Multi-file failure and rollback
# ---------------------------------------------------------------------------


def test_two_file_edit_applies_both_files(
    fake_transport: FakeChangeTransport, change_manager: ChangeManager
) -> None:
    """A clean multi-file changeset edits every file (regression for the
    leaked-loop-variable bug)."""
    result = asyncio.run(change_manager.apply(two_file_edit_request()))

    assert result.status is ChangeSetStatus.APPLIED
    assert fake_transport.files[PurePosixPath("/opt/payments/a.py")] == b"new-a\n"
    assert fake_transport.files[PurePosixPath("/opt/payments/b.py")] == b"new-b\n"


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


def test_new_file_rollback_removes_only_via_remove_file(
    change_manager: ChangeManager, fake_transport: FakeChangeTransport
) -> None:
    """A newly created file is rolled back with the typed single-file remove_file."""
    fake_transport.fail_rename_for(PurePosixPath("/opt/payments/b.py"))
    new_path = PurePosixPath("/opt/payments/a-new.txt")
    request = changeset(
        write("/opt/payments/a-new.txt", b"new content\n"),
        edit("/opt/payments/b.py", b"old-b\n", "old-b", "new-b"),
    )

    result = asyncio.run(change_manager.apply(request))

    assert result.status is ChangeSetStatus.ROLLED_BACK
    assert new_path not in fake_transport.files
    assert fake_transport.files[PurePosixPath("/opt/payments/b.py")] == b"old-b\n"
    assert f"remove:{new_path}" in fake_transport.calls
    assert all(
        "rm -r" not in call and "rm -fR" not in call for call in fake_transport.calls
    )


# ---------------------------------------------------------------------------
# FileWriteRequest semantics
# ---------------------------------------------------------------------------


def test_new_file_write_applies(
    fake_transport: FakeChangeTransport, change_manager: ChangeManager
) -> None:
    """A write to an absent target creates the file with no remote backup."""
    target = PurePosixPath("/opt/payments/generated.py")
    result = asyncio.run(change_manager.apply(changeset(write(str(target), b"x = 1\n"))))

    assert result.status is ChangeSetStatus.APPLIED
    assert fake_transport.files[target] == b"x = 1\n"


def test_new_file_write_rejects_existing_target(
    fake_transport: FakeChangeTransport, change_manager: ChangeManager
) -> None:
    """A write with ``expected_sha256=None`` rejects an existing target."""
    target = PurePosixPath("/opt/payments/generated.py")
    fake_transport.files[target] = b"x = 1\n"

    result = asyncio.run(
        change_manager.apply(changeset(write(str(target), b"y = 2\n")))
    )

    assert result.status is ChangeSetStatus.FAILED
    assert fake_transport.files[target] == b"x = 1\n"


# ---------------------------------------------------------------------------
# Replacement semantics
# ---------------------------------------------------------------------------


def test_overlapping_replacements_are_rejected(
    change_manager: ChangeManager, fake_transport: FakeChangeTransport
) -> None:
    """Overlapping replacement ranges are rejected before any write."""
    request = changeset(
        FileEditRequest(
            operation_id="op-overlap",
            incident_id="inc-1",
            project_id="payments",
            target_id="dev-a",
            service="payment-api",
            scope=HostScope(),
            path=PurePosixPath("/opt/payments/app.py"),
            expected_sha256=hashlib.sha256(b"old\n").hexdigest(),
            replacements=(
                TextReplacement(old_text="ol", new_text="x"),
                TextReplacement(old_text="ld\n", new_text="y\n"),
            ),
        )
    )

    result = asyncio.run(change_manager.apply(request))

    assert result.status is ChangeSetStatus.FAILED
    assert not any(call.startswith("write:") for call in fake_transport.calls)


# ---------------------------------------------------------------------------
# Protected paths and approvals
# ---------------------------------------------------------------------------


def test_is_protected_path_rules_are_shared_and_exhaustive() -> None:
    """The module-level rule mirrors ChangeManager's approval gate."""
    assert is_protected_path(PurePosixPath("/opt/app/.env")) is True
    assert is_protected_path(PurePosixPath("/opt/app/compose.yaml")) is True
    assert is_protected_path(PurePosixPath("/opt/app/Dockerfile")) is True
    assert is_protected_path(PurePosixPath("/etc/hosts")) is True
    assert is_protected_path(PurePosixPath("/etc/systemd/system/web.service")) is True
    assert is_protected_path(PurePosixPath("/opt/app/plain.conf")) is False
    protected = (PurePosixPath("/opt/app/protected"),)
    assert is_protected_path(PurePosixPath("/opt/app/protected/secrets.env"), protected) is True
    assert is_protected_path(PurePosixPath("/opt/app/other.env"), protected) is False


def test_protected_path_requires_approval(
    change_manager: ChangeManager, fake_transport: FakeChangeTransport
) -> None:
    """A .env change requires an approval and is rejected without one."""
    fake_transport.files[PurePosixPath("/opt/payments/.env")] = b"old\n"

    result = asyncio.run(
        change_manager.apply(changeset(edit("/opt/payments/.env", b"old\n", "old", "new")))
    )

    assert result.status is ChangeSetStatus.FAILED
    assert fake_transport.files[PurePosixPath("/opt/payments/.env")] == b"old\n"


def test_multi_protected_path_changeset_applies_with_single_approval(
    tmp_path: Path,
    fake_transport: FakeChangeTransport,
) -> None:
    """A changeset touching two protected paths applies with one approval (I3).

    The single-use approval is consumed exactly once for the combined intent,
    not once per protected file.
    """
    db_path = tmp_path / "multi-protected.db"

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    approval_store = ApprovalStore(connect)
    events = RuntimeEventStore(connect)
    broker = RuntimeEventBroker()
    approval_store.migrate()
    events.migrate()
    approvals = ApprovalService(
        approvals=approval_store,
        events=events,
        broker=broker,
    )

    store = ChangeSetStore(connect)
    store.migrate()
    vault = EncryptedBackupVault(tmp_path / "backups", tmp_path / "key.bin")
    service = ServiceRegistration(
        compose_service="payment-api",
        container_names=("payments-api-1",),
        allowed_host_paths=(PurePosixPath("/opt/payments"),),
        allowed_container_paths=(PurePosixPath("/app"),),
    )
    manager = ChangeManager(
        store=store,
        vault=vault,
        approvals=approvals,
        policy=RemotePathPolicy(service),
        transport=fake_transport,
    )

    fake_transport.files[PurePosixPath("/opt/payments/.env")] = b"KEY=old\n"
    fake_transport.files[PurePosixPath("/opt/payments/compose.yaml")] = (
        b"services:\n  web: {}\n"
    )
    request = changeset(
        edit("/opt/payments/.env", b"KEY=old\n", "KEY=old", "KEY=new"),
        edit(
            "/opt/payments/compose.yaml",
            b"services:\n  web: {}\n",
            "web",
            "api",
        ),
    )

    intent = protected_paths_intent(
        changeset_id="chg-1",
        target_id="dev-a",
        service="payment-api",
        paths=(
            PurePosixPath("/opt/payments/.env"),
            PurePosixPath("/opt/payments/compose.yaml"),
        ),
    )
    record = asyncio.run(approvals.request(intent))
    asyncio.run(approvals.approve(record.approval_id))

    result = asyncio.run(manager.apply(request, approval_id=record.approval_id))

    assert result.status is ChangeSetStatus.APPLIED
    assert fake_transport.files[PurePosixPath("/opt/payments/.env")] == b"KEY=new\n"
    assert fake_transport.files[PurePosixPath("/opt/payments/compose.yaml")] == (
        b"services:\n  api: {}\n"
    )

    # The single-use approval was consumed exactly once for the combined intent.
    after = approval_store.get(record.approval_id)
    assert after is not None
    assert after.status is ApprovalStatus.CONSUMED
    assert after.consumed_at is not None


def test_interrupts_service_fails_closed_when_project_lookup_fails(
    change_manager: ChangeManager,
) -> None:
    """A project-lookup failure treats the changeset as service-interrupting (I4)."""
    class _BrokenProjects:
        def get(self, project_id: str):  # noqa: ARG002
            raise RuntimeError("registry unavailable")

    manager = ChangeManager(
        store=change_manager._store,
        vault=change_manager._vault,
        projects=_BrokenProjects(),
        policy=change_manager._policy,
        transport=change_manager._transport,
    )
    changeset_id = seed_applied_changeset(
        manager._store, remote_path="/opt/payments/app.py"
    )
    seeded = manager._store.get(changeset_id)
    assert seeded is not None
    assert manager.interrupts_service(seeded) is True


# ---------------------------------------------------------------------------
# verify() and rollback()
# ---------------------------------------------------------------------------


def test_verify_transitions_applied_to_verified(
    change_manager: ChangeManager, fake_transport: FakeChangeTransport
) -> None:
    asyncio.run(change_manager.apply(single_file_changeset("/opt/payments/app.py")))

    asyncio.run(change_manager.verify("chg-1", "verified"))

    assert change_manager._store.get("chg-1").status is ChangeSetStatus.VERIFIED


def test_verify_failure_transitions_to_failed(
    change_manager: ChangeManager, fake_transport: FakeChangeTransport
) -> None:
    asyncio.run(change_manager.apply(single_file_changeset("/opt/payments/app.py")))

    asyncio.run(change_manager.verify("chg-1", "failed"))

    assert change_manager._store.get("chg-1").status is ChangeSetStatus.FAILED


def test_rollback_restores_applied_changeset(
    change_manager: ChangeManager, fake_transport: FakeChangeTransport
) -> None:
    asyncio.run(change_manager.apply(single_file_changeset("/opt/payments/app.py")))
    assert fake_transport.files[PurePosixPath("/opt/payments/app.py")] == b"new\n"

    asyncio.run(change_manager.rollback("chg-1"))

    assert fake_transport.files[PurePosixPath("/opt/payments/app.py")] == b"old\n"
    assert change_manager._store.get("chg-1").status is ChangeSetStatus.ROLLED_BACK


def test_rollback_validated_changeset_transitions_to_rolled_back(
    change_manager: ChangeManager,
) -> None:
    """Rolling back a VALIDATED changeset records ROLLED_BACK (I5)."""
    changeset_id = seed_applied_changeset(change_manager._store)
    change_manager._store.transition(changeset_id, ChangeSetStatus.VALIDATED)
    assert change_manager._store.get(changeset_id).status is ChangeSetStatus.VALIDATED

    asyncio.run(change_manager.rollback(changeset_id))

    assert change_manager._store.get(changeset_id).status is ChangeSetStatus.ROLLED_BACK


def test_rollback_protected_path_requires_approval(
    change_manager: ChangeManager,
) -> None:
    """A service-interrupting rollback is gated at the domain layer."""
    changeset_id = seed_applied_changeset(
        change_manager._store, remote_path="/opt/payments/.env"
    )

    with pytest.raises(ChangeRollbackError, match="approval"):
        asyncio.run(change_manager.rollback(changeset_id))

    # The changeset is untouched because the gate fires before any transport call.
    assert change_manager._store.get(changeset_id).status is ChangeSetStatus.APPLIED


def test_rollback_transport_failure_does_not_consume_approval(tmp_path: Path) -> None:
    """A transport-resolution failure must not burn a single-use approval."""
    db_path = tmp_path / "rollback.db"

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    approval_store = ApprovalStore(connect)
    events = RuntimeEventStore(connect)
    broker = RuntimeEventBroker()
    approval_store.migrate()
    events.migrate()

    approvals = ApprovalService(
        approvals=approval_store,
        events=events,
        broker=broker,
    )
    changeset_id = "chs-rollback-transport"
    intent = {
        "kind": "rollback",
        "changeset_id": changeset_id,
        "target_id": "dev-a",
        "service": "payment-api",
    }
    record = asyncio.run(approvals.request(intent))
    asyncio.run(approvals.approve(record.approval_id))

    store = ChangeSetStore(connect)
    store.migrate()
    seed_applied_changeset(
        store, changeset_id=changeset_id, remote_path="/opt/payments/.env"
    )
    vault = EncryptedBackupVault(tmp_path / "backups", tmp_path / "key.bin")

    # Sessions configured but no target registered -> transport resolution fails.
    manager = ChangeManager(
        store=store,
        vault=vault,
        approvals=approvals,
        sessions=SessionManager(FakeTransportFactory()),
        targets={},
    )

    with pytest.raises(ChangeApplyError):
        asyncio.run(manager.rollback(changeset_id, approval_id=record.approval_id))

    # The approval is still APPROVED and unconsumed, and the changeset stays APPLIED.
    after = approval_store.get(record.approval_id)
    assert after is not None
    assert after.status is ApprovalStatus.APPROVED
    assert after.consumed_at is None
    assert store.get(changeset_id).status is ChangeSetStatus.APPLIED


# ---------------------------------------------------------------------------
# Container backend
# ---------------------------------------------------------------------------


def test_container_backend_uses_fixed_docker_argv(
    fake_transport: FakeChangeTransport,
) -> None:
    """Container file operations use fixed docker exec argv templates."""
    backend = ContainerFileBackend(fake_transport, "payments-api-1")

    asyncio.run(backend.read_bytes(PurePosixPath("/app/app.py"), max_bytes=1024))
    asyncio.run(backend.copy_file(PurePosixPath("/app/a"), PurePosixPath("/app/b")))

    argv = fake_transport.run_argv_calls
    assert ("docker", "exec", "payments-api-1", "cat", "--", "/app/app.py") in argv
    assert (
        "docker",
        "exec",
        "payments-api-1",
        "cp",
        "--preserve",
        "--",
        "/app/a",
        "/app/b",
    ) in argv


def test_container_scope_changeset_applies(
    change_manager: ChangeManager,
) -> None:
    """A container-scope changeset applies via the fixed docker backend."""
    transport = FakeChangeTransport()
    transport.container_files[PurePosixPath("/app/app.py")] = b"old\n"
    container_req = ChangeSetRequest(
        changeset_id="chg-c",
        files=(
            FileEditRequest(
                operation_id="op-app.py",
                incident_id="inc-1",
                project_id="payments",
                target_id="dev-a",
                service="payment-api",
                scope=ContainerScope(container="payments-api-1"),
                path=PurePosixPath("/app/app.py"),
                expected_sha256=hashlib.sha256(b"old\n").hexdigest(),
                replacements=(TextReplacement(old_text="old", new_text="new"),),
            ),
        ),
        verification_plan="verify service behavior",
        rollback_plan="restore the backup",
    )

    manager = ChangeManager(
        store=change_manager._store,
        vault=change_manager._vault,
        policy=RemotePathPolicy(
            ServiceRegistration(
                compose_service="payment-api",
                container_names=("payments-api-1",),
                allowed_host_paths=(PurePosixPath("/opt/payments"),),
                allowed_container_paths=(PurePosixPath("/app"),),
            )
        ),
        transport=transport,
    )

    result = asyncio.run(manager.apply(container_req))

    assert result.status is ChangeSetStatus.APPLIED
    assert transport.container_files[PurePosixPath("/app/app.py")] == b"new\n"
