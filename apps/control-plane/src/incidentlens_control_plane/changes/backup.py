"""AES-GCM encrypted local backup vault."""

from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field


class BackupIntegrityError(Exception):
    """Raised when ciphertext tampering is detected during load."""


class BackupReference(BaseModel):
    """Opaque reference to an encrypted backup stored on disk."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_path: Path
    sha256: str = Field(min_length=64, max_length=64)


def _sanitize_segment(segment: str) -> str:
    """Sanitize a path segment to prevent traversal attacks."""
    return segment.replace("/", "_").replace("..", "__").replace("\\", "_")


class EncryptedBackupVault:
    """Stores file content as AES-256-GCM encrypted blobs with integrity verification."""

    def __init__(self, vault_root: Path, key_path: Path) -> None:
        self._vault_root = vault_root
        self._key_path = key_path
        self._key = self._load_or_create_key()

    def _load_or_create_key(self) -> bytes:
        """Load existing key or generate a new 256-bit AES-GCM key atomically."""
        if self._key_path.exists():
            return self._key_path.read_bytes()

        new_key = AESGCM.generate_key(bit_length=256)

        # Atomically create the key file with restricted permissions
        fd = os.open(str(self._key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, new_key)
        finally:
            os.close(fd)

        return new_key

    def store(
        self,
        target_id: str,
        incident_id: str,
        changeset_id: str,
        remote_path: PurePosixPath,
        content: bytes,
    ) -> BackupReference:
        """Encrypt and store content, returning a reference for later retrieval."""
        sha256_hash = hashlib.sha256(content).hexdigest()

        # Sanitize ID segments for safe path construction
        safe_target = _sanitize_segment(target_id)
        safe_incident = _sanitize_segment(incident_id)
        safe_changeset = _sanitize_segment(changeset_id)

        # Use SHA-256 of the remote path as filename to prevent traversal
        remote_path_hash = hashlib.sha256(str(remote_path).encode("utf-8")).hexdigest()[:16]

        # Build the file path under vault root
        file_dir = self._vault_root / safe_target / safe_incident / safe_changeset
        file_dir.mkdir(parents=True, exist_ok=True)
        # Restrict directory permissions
        os.chmod(str(file_dir), 0o700)

        file_path = file_dir / f"{remote_path_hash}.enc"

        # Build authenticated metadata
        metadata = {
            "target_id": target_id,
            "incident_id": incident_id,
            "changeset_id": changeset_id,
            "remote_path": str(remote_path),
            "plaintext_sha256": sha256_hash,
        }

        aesgcm = AESGCM(self._key)
        nonce = os.urandom(12)
        aad = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ciphertext = aesgcm.encrypt(nonce, content, aad)

        # Store: aad_len(4 bytes) + aad + nonce(12 bytes) + ciphertext
        aad_len = struct.pack("!I", len(aad))
        blob = aad_len + aad + nonce + ciphertext
        fd = os.open(str(file_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)

        return BackupReference(local_path=file_path, sha256=sha256_hash)

    def load(self, reference: BackupReference) -> bytes:
        """Load and decrypt a backup, verifying integrity."""
        blob = reference.local_path.read_bytes()

        # Minimum size: 4 (aad_len) + 12 (nonce) + 16 (AES-GCM tag + min ciphertext)
        if len(blob) < 32:
            raise BackupIntegrityError("Backup file is too small to contain valid ciphertext")

        # Read aad_len, aad, nonce, ciphertext
        aad_len = struct.unpack("!I", blob[:4])[0]
        if len(blob) < 4 + aad_len + 12:
            raise BackupIntegrityError("Backup file is corrupted: truncated AAD or nonce")

        aad = blob[4 : 4 + aad_len]
        nonce = blob[4 + aad_len : 4 + aad_len + 12]
        ciphertext = blob[4 + aad_len + 12 :]

        aesgcm = AESGCM(self._key)

        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
        except Exception as exc:
            raise BackupIntegrityError(
                f"Decryption failed - backup may be tampered: {exc}"
            ) from exc

        # Verify integrity via SHA-256
        actual_sha256 = hashlib.sha256(plaintext).hexdigest()
        if actual_sha256 != reference.sha256:
            raise BackupIntegrityError(
                f"SHA-256 mismatch: expected {reference.sha256}, got {actual_sha256}"
            )

        return plaintext
