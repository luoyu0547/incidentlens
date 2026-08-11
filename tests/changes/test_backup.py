"""Tests for EncryptedBackupVault."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path, PurePosixPath

import pytest
from incidentlens_control_plane.changes.backup import (
    BackupIntegrityError,
    EncryptedBackupVault,
)


def test_backup_is_encrypted_and_round_trips(tmp_path: Path) -> None:
    vault = EncryptedBackupVault(tmp_path / "backups", tmp_path / "backup.key")
    original = b"DATABASE_PASSWORD=super-secret\n"

    reference = vault.store(
        target_id="dev-a",
        incident_id="inc-1",
        changeset_id="chg-1",
        remote_path=PurePosixPath("/opt/payments/.env"),
        content=original,
    )

    assert reference.sha256 == hashlib.sha256(original).hexdigest()
    assert reference.local_path.read_bytes() != original
    assert vault.load(reference) == original
    assert stat.S_IMODE(reference.local_path.stat().st_mode) == 0o600


def test_backup_key_file_mode_is_600(tmp_path: Path) -> None:
    vault = EncryptedBackupVault(tmp_path / "backups", tmp_path / "backup.key")
    original = b"some content"

    vault.store(
        target_id="dev-a",
        incident_id="inc-1",
        changeset_id="chg-1",
        remote_path=PurePosixPath("/opt/app/.env"),
        content=original,
    )

    key_path = tmp_path / "backup.key"
    assert key_path.exists()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_backup_paths_are_under_vault_root(tmp_path: Path) -> None:
    vault_root = tmp_path / "backups"
    vault = EncryptedBackupVault(vault_root, tmp_path / "backup.key")
    original = b"content"

    reference = vault.store(
        target_id="dev-a",
        incident_id="inc-1",
        changeset_id="chg-1",
        remote_path=PurePosixPath("/opt/app/config.yaml"),
        content=original,
    )

    resolved = reference.local_path.resolve()
    assert str(resolved).startswith(str(vault_root.resolve()))


def test_backup_path_segments_are_sanitized(tmp_path: Path) -> None:
    vault = EncryptedBackupVault(tmp_path / "backups", tmp_path / "backup.key")
    original = b"content"

    reference = vault.store(
        target_id="../escape-target",
        incident_id="../escape-incident",
        changeset_id="../escape-changeset",
        remote_path=PurePosixPath("/opt/app/file.txt"),
        content=original,
    )

    resolved = reference.local_path.resolve()
    vault_resolved = (tmp_path / "backups").resolve()
    assert str(resolved).startswith(str(vault_resolved))
    # No traversal segments in the path parts
    for part in reference.local_path.relative_to(vault_resolved).parts:
        assert part != ".."


def test_ciphertext_tampering_raises_integrity_error(tmp_path: Path) -> None:
    vault = EncryptedBackupVault(tmp_path / "backups", tmp_path / "backup.key")
    original = b"secret data"

    reference = vault.store(
        target_id="dev-a",
        incident_id="inc-1",
        changeset_id="chg-1",
        remote_path=PurePosixPath("/opt/app/.env"),
        content=original,
    )

    # Tamper with the ciphertext by flipping bytes
    ciphertext = reference.local_path.read_bytes()
    tampered = bytearray(ciphertext)
    tampered[0] ^= 0xFF
    reference.local_path.write_bytes(bytes(tampered))

    with pytest.raises(BackupIntegrityError):
        vault.load(reference)
