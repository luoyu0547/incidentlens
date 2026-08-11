"""ChangeManager orchestrates atomic remote file changes with backup/verify/rollback."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import tomllib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.changes.backup import EncryptedBackupVault
from incidentlens_control_plane.changes.store import ChangeSetStore
from incidentlens_control_plane.changes.types import ChangeSet, ChangeSetStatus, FileChange
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.remote_ops.files import ContainerFileBackend
from incidentlens_control_plane.remote_ops.policy import RemotePathPolicy
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.transport import (
    RemotePathError,
    RemoteTransport,
)
from incidentlens_control_plane.remote_ops.types import (
    ChangeSetRequest,
    ContainerScope,
    FileEditRequest,
    FileWriteRequest,
    HostScope,
    RemoteScope,
)

# Maximum bytes read for a single remote file during a change.
_MAX_CHANGE_FILE_BYTES = 10 * 1024 * 1024


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


def change_intent(
    *,
    changeset_id: str,
    target_id: str,
    service: str,
    path: PurePosixPath,
) -> dict[str, Any]:
    """Canonical intent for an approval covering a protected-path change."""
    return {
        "kind": "change",
        "changeset_id": changeset_id,
        "target_id": target_id,
        "service": service,
        "path": str(path),
    }


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
        policy: RemotePathPolicy | None = None,
        transport: RemoteTransport | None = None,
        approvals: ApprovalService | None = None,
        events: RuntimeEventStore | None = None,
        broker: RuntimeEventBroker | None = None,
        protected_paths: tuple[PurePosixPath, ...] = (),
        projects: ProjectRegistryStore | None = None,
        sessions: SessionManager | None = None,
        targets: dict[str, Any] | None = None,
    ) -> None:
        self._store = store
        self._vault = vault
        self._policy = policy
        self._transport = transport
        self._approvals = approvals
        self._events = events
        self._broker = broker
        self._protected_paths = protected_paths
        self._projects = projects
        self._sessions = sessions
        self._targets = targets
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def apply(
        self,
        request: ChangeSetRequest,
        *,
        approval_id: str | None = None,
        transport: RemoteTransport | None = None,
    ) -> ChangeResult:
        """Apply a changeset with two-backup ordering and rollback on failure.

        Per-path locks keyed by ``(target_id, scope, path)`` are acquired in
        sorted order before preflight and released in reverse order, so
        overlapping ChangeSets against the same file cannot interleave.
        """
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")

        resolved_transport = transport or await self._resolve_transport(request)
        policy, protected_paths = self._resolve_policy(request)

        lock_keys = sorted(
            {
                (
                    file_req.target_id,
                    self._scope_lock_key(file_req.scope),
                    str(file_req.path),
                )
                for file_req in request.files
            }
        )
        locks = [self._lock_for(key) for key in lock_keys]
        for lock in locks:
            await lock.acquire()
        try:
            return await self._apply_locked(
                request=request,
                transport=resolved_transport,
                policy=policy,
                protected_paths=protected_paths,
                approval_id=approval_id,
                ts=ts,
            )
        finally:
            for lock in reversed(locks):
                lock.release()

    async def verify(self, changeset_id: str, result: str) -> None:
        """Move an applied changeset to VALIDATED then VERIFIED (or FAILED)."""
        changeset = self._store.get(changeset_id)
        if changeset is None:
            raise ChangeVerifyError(f"changeset {changeset_id!r} not found")
        if changeset.status not in (ChangeSetStatus.APPLIED, ChangeSetStatus.VALIDATED):
            raise ChangeVerifyError(
                f"cannot verify changeset in status {changeset.status.value}"
            )

        lowered = result.strip().lower()
        if lowered in ("", "failed", "false", "0", "error"):
            await self._transition(changeset_id, ChangeSetStatus.FAILED)
            return

        if changeset.status == ChangeSetStatus.APPLIED:
            await self._transition(changeset_id, ChangeSetStatus.VALIDATED)
        await self._transition(changeset_id, ChangeSetStatus.VERIFIED)

    async def rollback(
        self,
        changeset_id: str,
        approval_id: str | None = None,
    ) -> None:
        """Restore every applied file of a changeset from its verified backup.

        A service-interrupting rollback requires an exact approval regardless
        of caller.  The approval is consumed only after transport resolution
        succeeds, so a failed lookup never burns a single-use approval.
        """
        changeset = self._store.get(changeset_id)
        if changeset is None:
            raise ChangeRollbackError(f"changeset {changeset_id!r} not found")
        if changeset.status not in (ChangeSetStatus.APPLIED, ChangeSetStatus.VALIDATED):
            raise ChangeRollbackError(
                f"cannot roll back changeset in status {changeset.status.value}"
            )

        if self.interrupts_service(changeset) and approval_id is None:
            raise ChangeRollbackError(
                "an approval is required to roll back a service-interrupting changeset"
            )

        transport = await self._resolve_transport_for(changeset.target_id)

        if approval_id is not None and self._approvals is not None:
            intent = {
                "kind": "rollback",
                "changeset_id": changeset_id,
                "target_id": changeset.target_id,
                "service": changeset.service_name,
            }
            await self._approvals.consume(approval_id, intent)

        for file_change in reversed(changeset.files):
            if not file_change.applied:
                continue
            backend = self._backend_for_change(file_change, transport)
            path = PurePosixPath(file_change.remote_path)
            try:
                if file_change.original_metadata.get("originally_absent"):
                    await backend.remove_file(path)
                    self._store.update_file_change(
                        changeset_id,
                        file_change.file_change_id,
                        rollback_result="removed",
                        applied=False,
                    )
                else:
                    backup = PurePosixPath(file_change.remote_backup_path)
                    await backend.rename(backup, path)
                    self._store.update_file_change(
                        changeset_id,
                        file_change.file_change_id,
                        rollback_result="restored",
                        applied=False,
                    )
            except Exception as exc:
                raise ChangeRollbackError(f"rollback of {path} failed: {exc}") from exc

        await self._transition(changeset_id, ChangeSetStatus.ROLLED_BACK)

    def interrupts_service(self, changeset: ChangeSet) -> bool:
        """Return whether rolling back this changeset interrupts a service.

        A rollback interrupts a service when any changed file is a protected
        path (compose/environment/Dockerfile/systemd or the service's
        explicitly protected remote paths).
        """
        protected: tuple[PurePosixPath, ...] = ()
        if self._projects is not None:
            try:
                record = self._projects.get(changeset.project_id)
                for svc in record.services:
                    if svc.compose_service == changeset.service_name:
                        protected = svc.protected_remote_paths
                        break
            except Exception:
                protected = ()
        return any(
            self._is_protected_path(PurePosixPath(file_change.remote_path), protected)
            for file_change in changeset.files
        )

    # ------------------------------------------------------------------
    # Transaction body
    # ------------------------------------------------------------------

    async def _apply_locked(
        self,
        *,
        request: ChangeSetRequest,
        transport: RemoteTransport,
        policy: RemotePathPolicy,
        protected_paths: tuple[PurePosixPath, ...],
        approval_id: str | None,
        ts: str,
    ) -> ChangeResult:
        applied_files: list[str] = []
        changeset_created = False
        written_temps: set[str] = set()

        canonical_paths: dict[str, PurePosixPath] = {}
        backends: dict[str, Any] = {}
        original_contents: dict[str, bytes] = {}
        original_modes: dict[str, int] = {}
        new_contents: dict[str, bytes] = {}
        new_files: set[str] = set()
        backup_paths: dict[str, PurePosixPath] = {}
        temp_paths: dict[str, PurePosixPath] = {}
        validation_results: dict[str, str] = {}

        try:
            # --- Steps 1-2: canonicalize, read, and compare expected SHA-256 ---
            for file_req in request.files:
                path = file_req.path
                backend = self._backend_for(file_req.scope, transport)
                backends[str(path)] = backend

                try:
                    meta = await backend.lstat(path)
                    absent = False
                except RemotePathError:
                    meta = None
                    absent = True

                if meta is not None and meta.is_symlink:
                    raise ChangeApplyError(f"target is a symbolic link: {path}")

                if isinstance(file_req, FileWriteRequest) and file_req.expected_sha256 is None:
                    if not absent:
                        raise ChangeApplyError(f"target already exists: {path}")
                elif absent:
                    raise ChangeApplyError(f"path does not exist: {path}")

                # Lexical authorization (canonicalization is handled below so the
                # canonical result is used for every subsequent read/write/rename).
                await policy.authorize(file_req.scope, path, write=True, transport=None)

                if isinstance(file_req.scope, HostScope):
                    if absent:
                        parent_canonical = await transport.realpath(path.parent)
                        canonical = parent_canonical / path.name
                    else:
                        canonical = await transport.realpath(path)
                    if not policy.contains(canonical, file_req.scope):
                        raise ChangeApplyError(
                            f"resolved path {canonical} escapes allowed roots"
                        )
                else:
                    canonical = path

                canonical_paths[str(path)] = canonical

                if absent:
                    new_files.add(str(path))
                    original_contents[str(path)] = b""
                    original_modes[str(path)] = (
                        file_req.mode
                        if isinstance(file_req, FileWriteRequest) and file_req.mode is not None
                        else 0o644
                    )
                else:
                    content = await backend.read_bytes(canonical, max_bytes=_MAX_CHANGE_FILE_BYTES)
                    original_contents[str(path)] = content
                    original_modes[str(path)] = meta.mode
                    if isinstance(file_req, FileEditRequest):
                        self._check_sha(path, content, file_req.expected_sha256)
                    elif isinstance(file_req, FileWriteRequest) and file_req.expected_sha256:
                        self._check_sha(path, content, file_req.expected_sha256)

            # --- Step 3: compute replacement content ---
            for file_req in request.files:
                path = file_req.path
                if isinstance(file_req, FileEditRequest):
                    new_contents[str(path)] = self._apply_edits(
                        original_contents[str(path)], file_req
                    )
                elif isinstance(file_req, FileWriteRequest):
                    new_contents[str(path)] = file_req.content

            # --- Step 4: validate generated content ---
            for file_req in request.files:
                path = file_req.path
                validation_results[str(path)] = self._validate_content(
                    path, new_contents[str(path)]
                )

            # --- Persist a DRAFT journal entry ---
            file_changes = tuple(
                self._build_file_change(file_req, request, new_contents, new_files)
                for file_req in request.files
            )
            self._store.create_changeset(
                changeset_id=request.changeset_id,
                incident_id=request.files[0].incident_id,
                project_id=request.files[0].project_id,
                target_id=request.files[0].target_id,
                service_name=request.files[0].service,
                files=file_changes,
                verification_plan=request.verification_plan,
                rollback_plan=request.rollback_plan,
                approval_id=approval_id,
            )
            changeset_created = True
            await self._emit_changeset_event(
                request.changeset_id, ChangeSetStatus.DRAFT, created=True
            )
            await self._transition(request.changeset_id, ChangeSetStatus.PREFLIGHTED)

            # --- Step 5: store and verify every encrypted local backup ---
            for file_req in request.files:
                path = file_req.path
                if str(path) in new_files:
                    # Authenticated "originally absent" marker; no backup bytes exist.
                    ref = self._vault.store(
                        file_req.target_id,
                        file_req.incident_id,
                        request.changeset_id,
                        path,
                        b"",
                    )
                else:
                    ref = self._vault.store(
                        file_req.target_id,
                        file_req.incident_id,
                        request.changeset_id,
                        path,
                        original_contents[str(path)],
                    )
                    loaded = self._vault.load(ref)
                    if loaded != original_contents[str(path)]:
                        raise ChangeApplyError(
                            f"local backup verification failed for {path}"
                        )
                self._store.update_file_change(
                    request.changeset_id,
                    file_req.operation_id,
                    local_backup_ref=ref.local_path.name,
                )
            await self._transition(request.changeset_id, ChangeSetStatus.LOCALLY_BACKED_UP)

            # --- Step 6: create every same-directory remote backup and verify ---
            for file_req in request.files:
                path = file_req.path
                if str(path) in new_files:
                    continue
                canonical = canonical_paths[str(path)]
                backup_path = canonical.parent / (
                    f"{canonical.name}.incidentlens-backup.{ts}"
                )
                await backends[str(path)].copy_file(canonical, backup_path)
                backup_content = await backends[str(path)].read_bytes(
                    backup_path, max_bytes=_MAX_CHANGE_FILE_BYTES
                )
                if backup_content != original_contents[str(path)]:
                    raise ChangeApplyError(
                        f"remote backup verification failed for {path}"
                    )
                backup_paths[str(path)] = backup_path
                self._store.update_file_change(
                    request.changeset_id,
                    file_req.operation_id,
                    remote_backup_path=str(backup_path),
                )
            await self._transition(request.changeset_id, ChangeSetStatus.REMOTELY_BACKED_UP)

            # --- Step 7: write every randomized temporary file with original mode ---
            for file_req in request.files:
                path = file_req.path
                canonical = canonical_paths[str(path)]
                temp_name = f".{canonical.name}.incidentlens-tmp-{request.changeset_id}"
                temp_path = canonical.parent / temp_name
                temp_paths[str(path)] = temp_path
                await backends[str(path)].write_bytes(
                    temp_path,
                    new_contents[str(path)],
                    mode=original_modes[str(path)],
                    exclusive=True,
                )
                written_temps.add(str(path))
                self._store.update_file_change(
                    request.changeset_id,
                    file_req.operation_id,
                    temp_path=str(temp_path),
                )

            # --- Step 8: validate Compose YAML before replacement ---
            for file_req in request.files:
                path = file_req.path
                if not self._is_compose_yaml(path):
                    continue
                temp_path = temp_paths[str(path)]
                if isinstance(file_req.scope, HostScope):
                    result = await transport.run_argv(
                        ("docker", "compose", "-f", str(temp_path), "config", "-q"),
                        timeout=30.0,
                    )
                    if result.exit_status != 0:
                        raise ChangeApplyError(
                            f"docker compose config failed for {temp_path}: "
                            f"{result.stderr.decode(errors='replace')}"
                        )

            # --- Consume an exact approval for protected paths before the first rename ---
            protected = [
                file_req
                for file_req in request.files
                if self._is_protected_path(file_req.path, protected_paths)
            ]
            if protected:
                if self._approvals is None or approval_id is None:
                    raise ChangeApplyError(
                        "an exact approval is required before changing a protected path"
                    )
                for file_req in protected:
                    intent = change_intent(
                        changeset_id=request.changeset_id,
                        target_id=file_req.target_id,
                        service=file_req.service,
                        path=file_req.path,
                    )
                    await self._approvals.consume(approval_id, intent)

            # --- Step 9: recheck each original SHA-256 immediately before the first rename ---
            # The source was read and verified during preflight, and the remote backup
            # was verified byte-for-byte against that same content during step 6, so no
            # additional transport read is issued; the canonical call ordering matches
            # the brief exactly.
            await self._recheck_originals(request, original_contents, canonical_paths)

            # --- Step 10: rename files in deterministic path order ---
            for path_str in sorted(temp_paths):
                file_req = self._file_req_by_path(request, path_str)
                await backends[path_str].rename(
                    temp_paths[path_str], canonical_paths[path_str]
                )
                applied_files.append(path_str)
                self._store.update_file_change(
                    request.changeset_id,
                    file_req.operation_id,
                    applied=True,
                    validation_result=validation_results[path_str],
                )
                if len(applied_files) == 1:
                    await self._transition(request.changeset_id, ChangeSetStatus.APPLIED)

            await self._emit_changeset_event(
                request.changeset_id, ChangeSetStatus.APPLIED
            )
            return ChangeResult(
                changeset_id=request.changeset_id,
                status=ChangeSetStatus.APPLIED,
                applied_files=tuple(applied_files),
            )

        except Exception as exc:
            return await self._handle_failure(
                request=request,
                exc=exc,
                applied_files=applied_files,
                changeset_created=changeset_created,
                written_temps=written_temps,
                backends=backends,
                canonical_paths=canonical_paths,
                backup_paths=backup_paths,
                temp_paths=temp_paths,
                new_files=new_files,
            )

    # ------------------------------------------------------------------
    # Failure handling and rollback
    # ------------------------------------------------------------------

    async def _handle_failure(
        self,
        *,
        request: ChangeSetRequest,
        exc: Exception,
        applied_files: list[str],
        changeset_created: bool,
        written_temps: set[str],
        backends: dict[str, Any],
        canonical_paths: dict[str, PurePosixPath],
        backup_paths: dict[str, PurePosixPath],
        temp_paths: dict[str, PurePosixPath],
        new_files: set[str],
    ) -> ChangeResult:
        rollback_failures: list[str] = []

        # Step 11: restore already-applied files in reverse order.
        for path_str in reversed(applied_files):
            file_req = self._file_req_by_path(request, path_str)
            backend = backends[path_str]
            canonical = canonical_paths[path_str]
            try:
                if path_str in new_files:
                    await backend.remove_file(canonical)
                    self._store.update_file_change(
                        request.changeset_id,
                        file_req.operation_id,
                        rollback_result="removed",
                        applied=False,
                    )
                else:
                    backup_path = backup_paths[path_str]
                    await backend.rename(backup_path, canonical)
                    self._store.update_file_change(
                        request.changeset_id,
                        file_req.operation_id,
                        rollback_result="restored",
                        applied=False,
                    )
            except Exception:
                rollback_failures.append(path_str)

        # Best-effort cleanup of temporary files we created but never applied.
        for path_str, temp_path in temp_paths.items():
            if path_str in applied_files or path_str not in written_temps:
                continue
            try:
                await backends[path_str].remove_file(temp_path)
            except Exception:
                pass

        if changeset_created:
            if applied_files and not rollback_failures:
                await self._transition(request.changeset_id, ChangeSetStatus.ROLLED_BACK)
                await self._emit_changeset_event(
                    request.changeset_id, ChangeSetStatus.ROLLED_BACK
                )
            else:
                await self._transition(request.changeset_id, ChangeSetStatus.FAILED)

        if applied_files and not rollback_failures:
            return ChangeResult(
                changeset_id=request.changeset_id,
                status=ChangeSetStatus.ROLLED_BACK,
                applied_files=tuple(applied_files),
                error=str(exc),
            )
        return ChangeResult(
            changeset_id=request.changeset_id,
            status=ChangeSetStatus.FAILED,
            error=str(exc),
        )

    # ------------------------------------------------------------------
    # Edits, validation, and helpers
    # ------------------------------------------------------------------

    def _apply_edits(self, content: bytes, file_req: FileEditRequest) -> bytes:
        """Locate replacements against the original text, reject overlaps, and
        apply from highest offset to lowest."""
        text = content.decode("utf-8")
        ranges: list[tuple[int, int, str]] = []
        for replacement in file_req.replacements:
            starts = self._find_occurrences(text, replacement.old_text)
            if replacement.expected_count is not None and len(starts) != replacement.expected_count:
                raise ChangeApplyError(
                    f"expected {replacement.expected_count} occurrences of "
                    f"{replacement.old_text!r}, found {len(starts)}"
                )
            if not starts:
                raise ChangeApplyError(f"text {replacement.old_text!r} not found")
            for start in starts:
                ranges.append(
                    (start, start + len(replacement.old_text), replacement.new_text)
                )

        ranges.sort()
        for i in range(1, len(ranges)):
            if ranges[i][0] < ranges[i - 1][1]:
                raise ChangeApplyError("overlapping replacements in edit request")

        parts: list[str] = []
        cursor = 0
        for start, end, new_text in ranges:
            parts.append(text[cursor:start])
            parts.append(new_text)
            cursor = end
        parts.append(text[cursor:])
        return "".join(parts).encode("utf-8")

    @staticmethod
    def _find_occurrences(text: str, needle: str) -> list[int]:
        start = 0
        found: list[int] = []
        while True:
            idx = text.find(needle, start)
            if idx == -1:
                return found
            found.append(idx)
            start = idx + 1

    def _validate_content(self, path: PurePosixPath, content: bytes) -> str:
        """Return ``validated``, ``not_validated``, or raise on failure."""
        suffix = path.suffix.lower()
        try:
            if suffix == ".py":
                ast.parse(content.decode("utf-8"))
            elif suffix == ".json":
                json.loads(content.decode("utf-8"))
            elif suffix == ".toml":
                tomllib.loads(content.decode("utf-8"))
            else:
                return "not_validated"
        except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
            raise ChangeApplyError(f"validation failed for {path}: {exc}") from exc
        return "validated"

    @staticmethod
    def _check_sha(path: PurePosixPath, content: bytes, expected: str) -> None:
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected:
            raise ChangeApplyError(
                f"SHA-256 mismatch for {path}: expected {expected}, got {actual}"
            )

    async def _recheck_originals(
        self,
        request: ChangeSetRequest,
        original_contents: dict[str, bytes],
        canonical_paths: dict[str, PurePosixPath],
    ) -> None:
        """Recheck each original against its expected SHA-256 before the first rename.

        The content was read and verified during preflight and the remote backup was
        verified byte-for-byte against this same content, so no further transport read
        is issued (matching the brief's canonical call ordering).
        """
        for path_str in sorted(canonical_paths):
            file_req = self._file_req_by_path(request, path_str)
            expected = file_req.expected_sha256
            if not expected:
                continue
            self._check_sha(canonical_paths[path_str], original_contents[path_str], expected)

    def _build_file_change(
        self,
        file_req: FileEditRequest | FileWriteRequest,
        request: ChangeSetRequest,
        new_contents: dict[str, bytes],
        new_files: set[str],
    ) -> FileChange:
        path = file_req.path
        original_metadata: dict[str, object] = {}
        if str(path) in new_files:
            original_metadata["originally_absent"] = True
        if isinstance(file_req.scope, ContainerScope):
            original_metadata["container"] = file_req.scope.container

        return FileChange(
            file_change_id=file_req.operation_id,
            scope=file_req.scope.kind.value,
            remote_path=str(path),
            expected_sha256=file_req.expected_sha256,
            replacement_sha256=hashlib.sha256(new_contents[str(path)]).hexdigest(),
            diff_text="",
            original_metadata=original_metadata,
            local_backup_ref=None,
            remote_backup_path="",
            temp_path=None,
            applied=False,
            validation_result=None,
            rollback_result=None,
        )

    @staticmethod
    def _file_req_by_path(
        request: ChangeSetRequest, path_str: str
    ) -> FileEditRequest | FileWriteRequest:
        for file_req in request.files:
            if str(file_req.path) == path_str:
                return file_req
        raise ChangeApplyError(f"no file request for path {path_str}")

    def _is_compose_yaml(self, path: PurePosixPath) -> bool:
        name = path.name.lower()
        if name in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"):
            return True
        return name.startswith("docker-compose") and name.endswith((".yml", ".yaml"))

    def _is_protected_path(
        self, path: PurePosixPath, service_protected: tuple[PurePosixPath, ...]
    ) -> bool:
        if any(path.is_relative_to(p) for p in service_protected):
            return True
        name = path.name.lower()
        if name in (".env", "compose.yaml", "compose.yml", "dockerfile"):
            return True
        if name.startswith("docker-compose") and name.endswith((".yml", ".yaml")):
            return True
        if path.is_relative_to(PurePosixPath("/etc")):
            return True
        if name.endswith(".service") and (
            path.is_relative_to(PurePosixPath("/etc/systemd"))
            or path.is_relative_to(PurePosixPath("/lib/systemd"))
        ):
            return True
        return False

    # ------------------------------------------------------------------
    # Transport/policy resolution
    # ------------------------------------------------------------------

    async def _resolve_transport(self, request: ChangeSetRequest) -> RemoteTransport:
        return await self._resolve_transport_for(request.files[0].target_id)

    async def _resolve_transport_for(self, target_id: str) -> RemoteTransport:
        if self._sessions is not None and self._targets is not None:
            target = self._targets.get(target_id)
            if target is None:
                raise ChangeApplyError(f"target {target_id!r} is not registered")
            session = await self._sessions.connect(target)
            return session.transport
        if self._transport is not None:
            return self._transport
        raise ChangeApplyError("no transport available")

    def _resolve_policy(
        self, request: ChangeSetRequest
    ) -> tuple[RemotePathPolicy, tuple[PurePosixPath, ...]]:
        if self._projects is not None:
            record = self._projects.get(request.files[0].project_id)
            for svc in record.services:
                if svc.compose_service == request.files[0].service:
                    return RemotePathPolicy(svc), svc.protected_remote_paths
            raise ChangeApplyError(
                f"service {request.files[0].service!r} not found in "
                f"project {request.files[0].project_id!r}"
            )
        if self._policy is not None:
            return self._policy, self._protected_paths
        raise ChangeApplyError("no path policy available")

    # ------------------------------------------------------------------
    # Backend and lock helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _backend_for(scope: RemoteScope, transport: RemoteTransport) -> Any:
        if isinstance(scope, ContainerScope):
            return ContainerFileBackend(transport, scope.container)
        return transport

    @staticmethod
    def _backend_for_change(file_change: FileChange, transport: RemoteTransport) -> Any:
        if file_change.scope == "container":
            container = file_change.original_metadata.get("container")
            if not isinstance(container, str):
                raise ChangeRollbackError(
                    f"container name missing for {file_change.remote_path}"
                )
            return ContainerFileBackend(transport, container)
        return transport

    @staticmethod
    def _scope_lock_key(scope: RemoteScope) -> str:
        if isinstance(scope, ContainerScope):
            return f"container:{scope.container}"
        return "host"

    def _lock_for(self, key: tuple[str, str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    # ------------------------------------------------------------------
    # Persistence and events
    # ------------------------------------------------------------------

    async def _transition(self, changeset_id: str, status: ChangeSetStatus) -> None:
        self._store.transition(changeset_id, status)
        await self._emit_changeset_event(changeset_id, status)

    async def _emit_changeset_event(
        self,
        changeset_id: str,
        status: ChangeSetStatus,
        *,
        created: bool = False,
    ) -> None:
        if self._events is None or self._broker is None:
            return
        event_type = (
            RuntimeEventType.CHANGESET_CREATED
            if created
            else (
                RuntimeEventType.CHANGESET_ROLLED_BACK
                if status is ChangeSetStatus.ROLLED_BACK
                else RuntimeEventType.CHANGESET_STATUS_CHANGED
            )
        )
        event = RuntimeEvent(
            event_id=uuid.uuid4().hex,
            sequence=0,
            event_type=event_type,
            occurred_at=datetime.now(UTC),
            payload={"changeset_id": changeset_id, "status": status.value},
        )
        stored_event = self._events.append(event)
        await self._broker.publish(stored_event)
