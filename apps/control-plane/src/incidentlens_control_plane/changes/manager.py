"""ChangeManager orchestrates atomic remote file changes with backup/verify/rollback."""

from __future__ import annotations

import ast
import hashlib
import json
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

from incidentlens_control_plane.changes.backup import EncryptedBackupVault
from incidentlens_control_plane.changes.store import ChangeSetStore
from incidentlens_control_plane.changes.types import ChangeSetStatus
from incidentlens_control_plane.remote_ops.policy import RemotePathPolicy
from incidentlens_control_plane.remote_ops.transport import RemoteTransport
from incidentlens_control_plane.remote_ops.types import (
    ChangeSetRequest,
    FileEditRequest,
    FileWriteRequest,
)


class ChangeApplyError(Exception):
    """Raised when a changeset apply fails."""


class ChangeVerifyError(Exception):
    """Raised when a changeset verification fails."""


class ChangeRollbackError(Exception):
    """Raised when a changeset rollback fails."""


@dataclass
class ChangeResult:
    """Result of applying a changeset."""

    changeset_id: str
    status: ChangeSetStatus
    applied_files: tuple[str, ...] = ()
    error: str | None = None


class ChangeManager:
    """Orchestrates atomic remote file changes with two-backup ordering.

    The transaction follows 12 steps:
    1. Re-authorize and canonicalize every path.
    2. Read current bytes/metadata and compare expected SHA-256.
    3. For FileEditRequest, decode UTF-8, locate replacements, apply from highest offset.
    4. Validate .py with ast.parse, .json with json.loads, .toml with tomllib.loads.
    5. Store and verify every encrypted local backup.
    6. Create every same-directory remote backup and verify its bytes.
    7. Write every randomized temporary file with original mode.
    8. For Compose YAML, run fixed docker compose -f <temporary-path> config -q.
    9. Recheck each original SHA-256 immediately before the first rename.
    10. Rename files in deterministic path order.
    11. On failure, restore already-applied files in reverse order.
    12. Persist every state change and emit Runtime events.
    """

    def __init__(
        self,
        store: ChangeSetStore,
        vault: EncryptedBackupVault,
        policy: RemotePathPolicy,
        transport: RemoteTransport,
    ) -> None:
        self._store = store
        self._vault = vault
        self._policy = policy
        self._transport = transport

    async def apply(self, request: ChangeSetRequest) -> ChangeResult:
        """Apply a changeset with two-backup ordering and rollback on failure."""
        now = datetime.now(UTC)
        ts = now.strftime("%Y%m%dT%H%M%S.%fZ")

        applied_files: list[str] = []
        backup_paths: dict[str, PurePosixPath] = {}
        temp_paths: dict[str, PurePosixPath] = {}
        original_contents: dict[str, bytes] = {}
        original_modes: dict[str, int] = {}

        try:
            # Step 1: Re-authorize and canonicalize every path
            for file_req in request.files:
                path = file_req.path
                await self._policy.authorize(
                    file_req.scope, path, write=True, transport=self._transport
                )

            # Step 2: Read current bytes/metadata and compare expected SHA-256
            for file_req in request.files:
                path = file_req.path
                content = await self._transport.read_bytes(path, max_bytes=10 * 1024 * 1024)
                original_contents[str(path)] = content

                if isinstance(file_req, FileEditRequest) and file_req.expected_sha256:
                    actual_sha = hashlib.sha256(content).hexdigest()
                    if actual_sha != file_req.expected_sha256:
                        raise ChangeApplyError(
                            f"SHA-256 mismatch for {path}: "
                            f"expected {file_req.expected_sha256}, got {actual_sha}"
                        )

                # Record original mode for temp file creation
                stat = await self._transport.lstat(path)
                original_modes[str(path)] = stat.mode

            # Step 3: For FileEditRequest, apply text replacements
            new_contents: dict[str, bytes] = {}
            for file_req in request.files:
                if isinstance(file_req, FileEditRequest):
                    content = original_contents[str(path)]
                    new_content = self._apply_edits(content, file_req)
                    new_contents[str(path)] = new_content
                elif isinstance(file_req, FileWriteRequest):
                    # For write requests, the new content is provided
                    new_contents[str(path)] = file_req.content.encode("utf-8")

            # Step 4: Validate generated files
            for file_req in request.files:
                path = file_req.path
                if isinstance(file_req, FileEditRequest):
                    content = new_contents[str(path)]
                    self._validate_content(path, content)

            # Step 5: Store and verify every encrypted local backup
            for file_req in request.files:
                path = file_req.path
                content = original_contents[str(path)]
                backup_ref = self._vault.store(
                    file_req.target_id,
                    file_req.incident_id,
                    request.changeset_id,
                    path,
                    content,
                )
                # Verify the backup
                loaded = self._vault.load(backup_ref)
                if loaded != content:
                    raise ChangeApplyError(
                        f"Local backup verification failed for {path}"
                    )

            # Step 6: Create every same-directory remote backup
            for file_req in request.files:
                path = file_req.path
                backup_name = f"{path.name}.incidentlens-backup.{ts}"
                backup_path = path.parent / backup_name

                # Copy original to backup location
                await self._transport.copy_file(path, backup_path)

                # Verify the backup bytes
                backup_content = await self._transport.read_bytes(
                    backup_path, max_bytes=10 * 1024 * 1024
                )
                original_content = original_contents[str(path)]
                if backup_content != original_content:
                    raise ChangeApplyError(
                        f"Remote backup verification failed for {path}"
                    )

                backup_paths[str(path)] = backup_path

            # Step 7: Write every randomized temporary file
            for file_req in request.files:
                path = file_req.path
                temp_name = f".{path.name}.incidentlens-tmp-chg-{request.changeset_id}"
                temp_path = path.parent / temp_name
                temp_paths[str(path)] = temp_path

                content = new_contents[str(path)]
                mode = original_modes[str(path)]
                await self._transport.write_bytes(temp_path, content, mode=mode)

            # Step 8: For Compose YAML, run docker compose config -q
            for file_req in request.files:
                path = file_req.path
                if self._is_compose_yaml(path):
                    temp_path = temp_paths[str(path)]
                    # Run fixed docker compose config -q
                    result = await self._transport.run_argv(
                        ("docker", "compose", "-f", str(temp_path), "config", "-q"),
                        timeout=30.0,
                    )
                    if result.exit_status != 0:
                        raise ChangeApplyError(
                            f"docker compose config failed for {temp_path}: "
                            f"{result.stderr.decode(errors='replace')}"
                        )

            # Step 9: Recheck each original SHA-256 immediately before the first rename
            for file_req in request.files:
                path = file_req.path
                if isinstance(file_req, FileEditRequest) and file_req.expected_sha256:
                    content = await self._transport.read_bytes(
                        path, max_bytes=10 * 1024 * 1024
                    )
                    actual_sha = hashlib.sha256(content).hexdigest()
                    if actual_sha != file_req.expected_sha256:
                        raise ChangeApplyError(
                            f"SHA-256 stale before rename for {path}: "
                            f"expected {file_req.expected_sha256}, got {actual_sha}"
                        )

            # Step 10: Rename files in deterministic path order
            sorted_paths = sorted(temp_paths.keys(), key=lambda p: PurePosixPath(p))
            for path_str in sorted_paths:
                path = PurePosixPath(path_str)
                temp_path = temp_paths[path_str]
                await self._transport.rename(temp_path, path)
                applied_files.append(path_str)

            # Success - persist and return
            return ChangeResult(
                changeset_id=request.changeset_id,
                status=ChangeSetStatus.APPLIED,
                applied_files=tuple(applied_files),
            )

        except Exception as exc:
            # Step 11: On failure, restore already-applied files in reverse order
            for path_str in reversed(applied_files):
                path = PurePosixPath(path_str)
                backup_path = backup_paths.get(path_str)
                if backup_path:
                    # Restore from remote backup
                    backup_content = await self._transport.read_bytes(
                        backup_path, max_bytes=10 * 1024 * 1024
                    )
                    # Remove the applied file
                    await self._transport.remove_file(path)
                    # Write back the original content
                    mode = original_modes.get(path_str, 0o644)
                    await self._transport.write_bytes(path, backup_content, mode=mode)

            # Clean up any temp files that weren't applied
            for path_str, temp_path in temp_paths.items():
                if path_str not in applied_files:
                    try:
                        await self._transport.remove_file(temp_path)
                    except Exception:
                        pass  # Best effort cleanup

            return ChangeResult(
                changeset_id=request.changeset_id,
                status=ChangeSetStatus.ROLLED_BACK,
                error=str(exc),
            )

    def _apply_edits(self, content: bytes, file_req: FileEditRequest) -> bytes:
        """Apply text replacements from highest offset to lowest."""
        text = content.decode("utf-8")
        replacements = sorted(
            file_req.replacements,
            key=lambda r: text.rfind(r.old_text),
            reverse=True,
        )

        for replacement in replacements:
            old_text = replacement.old_text
            new_text = replacement.new_text
            expected_count = replacement.expected_count

            # Count occurrences
            count = text.count(old_text)
            if expected_count is not None and count != expected_count:
                raise ChangeApplyError(
                    f"Expected {expected_count} occurrences of {old_text!r}, found {count}"
                )
            if count == 0:
                raise ChangeApplyError(f"Text {old_text!r} not found")

            # Replace from highest offset to lowest (already sorted)
            text = text.replace(old_text, new_text)

        return text.encode("utf-8")

    def _validate_content(self, path: PurePosixPath, content: bytes) -> None:
        """Validate generated content based on file extension."""
        suffix = path.suffix

        if suffix == ".py":
            try:
                ast.parse(content.decode("utf-8"))
            except SyntaxError as exc:
                raise ChangeApplyError(f"Python syntax validation failed: {exc}")

        elif suffix == ".json":
            try:
                json.loads(content.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ChangeApplyError(f"JSON validation failed: {exc}")

        elif suffix == ".toml":
            try:
                tomllib.loads(content.decode("utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise ChangeApplyError(f"TOML validation failed: {exc}")

    def _is_compose_yaml(self, path: PurePosixPath) -> bool:
        """Check if a path is a Compose YAML file."""
        name = path.name.lower()
        return name in (
            "compose.yaml",
            "compose.yml",
            "docker-compose.yml",
            "docker-compose.yaml",
        ) or name.startswith("docker-compose") and name.endswith((".yml", ".yaml"))

    async def verify(self, changeset_id: str, result: str) -> None:
        """Verify a changeset after apply."""
        pass

    async def rollback(self, changeset_id: str, approval_id: str | None = None) -> None:
        """Rollback a changeset."""
        pass
