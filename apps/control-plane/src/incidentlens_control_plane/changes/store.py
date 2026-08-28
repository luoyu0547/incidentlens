"""SQLite-backed ChangeSet journal with enforced state transitions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from incidentlens_control_plane.changes.types import (
    _TERMINAL_STATES,
    _VALID_PREDECESSORS,
    ChangeSet,
    ChangeSetStatus,
    FileChange,
)


class ChangeSetNotFound(Exception):
    """Raised when a ChangeSet is not found by ID."""


class InvalidChangeTransition(Exception):
    """Raised when a state transition is not allowed from the current status."""


class ChangeSetStore:
    """SQLite-backed store for ChangeSet lifecycle management."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connection_factory = connection_factory

    def migrate(self) -> None:
        """Create the changeset and file_change tables."""
        with self._connection_factory() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS changesets (
                    changeset_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    verification_plan TEXT NOT NULL DEFAULT '',
                    rollback_plan TEXT NOT NULL DEFAULT '',
                    approval_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS file_changes (
                    file_change_id TEXT PRIMARY KEY,
                    changeset_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    remote_path TEXT NOT NULL,
                    expected_sha256 TEXT,
                    replacement_sha256 TEXT NOT NULL,
                    diff_text TEXT NOT NULL DEFAULT '',
                    original_metadata TEXT NOT NULL DEFAULT '{}',
                    local_backup_ref TEXT,
                    remote_backup_path TEXT NOT NULL DEFAULT '',
                    temp_path TEXT,
                    applied INTEGER NOT NULL DEFAULT 0,
                    validation_result TEXT,
                    rollback_result TEXT,
                    FOREIGN KEY (changeset_id) REFERENCES changesets(changeset_id)
                )
                """
            )
            conn.commit()

    def create(self, file_change: FileChange) -> ChangeSet:
        """Create a new ChangeSet in DRAFT status with a single file change."""
        now = datetime.now(UTC)
        changeset_id = f"chs-{now.strftime('%Y%m%d%H%M%S')}-{file_change.file_change_id}"

        changeset = ChangeSet(
            changeset_id=changeset_id,
            incident_id="pending",
            project_id="pending",
            target_id="pending",
            service_name="pending",
            files=(file_change,),
            status=ChangeSetStatus.DRAFT,
            created_at=now,
            updated_at=now,
        )

        with self._connection_factory() as conn:
            conn.execute(
                """
                INSERT INTO changesets
                    (changeset_id, incident_id, project_id, target_id, service_name,
                     status, verification_plan, rollback_plan, approval_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    changeset.changeset_id,
                    changeset.incident_id,
                    changeset.project_id,
                    changeset.target_id,
                    changeset.service_name,
                    changeset.status.value,
                    changeset.verification_plan,
                    changeset.rollback_plan,
                    changeset.approval_id,
                    changeset.created_at.isoformat(),
                    changeset.updated_at.isoformat(),
                ),
            )

            conn.execute(
                """
                INSERT INTO file_changes
                    (file_change_id, changeset_id, scope, remote_path,
                     expected_sha256, replacement_sha256, diff_text,
                     original_metadata, local_backup_ref, remote_backup_path,
                     temp_path, applied, validation_result, rollback_result)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_change.file_change_id,
                    changeset_id,
                    file_change.scope,
                    file_change.remote_path,
                    file_change.expected_sha256,
                    file_change.replacement_sha256,
                    file_change.diff_text,
                    json.dumps(file_change.original_metadata),
                    file_change.local_backup_ref,
                    file_change.remote_backup_path,
                    file_change.temp_path,
                    int(file_change.applied),
                    file_change.validation_result,
                    file_change.rollback_result,
                ),
            )
            conn.commit()

        return changeset

    def create_changeset(
        self,
        *,
        changeset_id: str,
        incident_id: str,
        project_id: str,
        target_id: str,
        service_name: str,
        files: tuple[FileChange, ...],
        verification_plan: str = "",
        rollback_plan: str = "",
        approval_id: str | None = None,
    ) -> ChangeSet:
        """Create a ChangeSet with an explicit ID and multiple file changes."""
        now = datetime.now(UTC)
        changeset = ChangeSet(
            changeset_id=changeset_id,
            incident_id=incident_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            files=files,
            status=ChangeSetStatus.DRAFT,
            created_at=now,
            updated_at=now,
            verification_plan=verification_plan,
            rollback_plan=rollback_plan,
            approval_id=approval_id,
        )

        with self._connection_factory() as conn:
            conn.execute(
                """
                INSERT INTO changesets
                    (changeset_id, incident_id, project_id, target_id, service_name,
                     status, verification_plan, rollback_plan, approval_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    changeset_id,
                    incident_id,
                    project_id,
                    target_id,
                    service_name,
                    ChangeSetStatus.DRAFT.value,
                    verification_plan,
                    rollback_plan,
                    approval_id,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            for file_change in files:
                conn.execute(
                    """
                    INSERT INTO file_changes
                        (file_change_id, changeset_id, scope, remote_path,
                         expected_sha256, replacement_sha256, diff_text,
                         original_metadata, local_backup_ref, remote_backup_path,
                         temp_path, applied, validation_result, rollback_result)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_change.file_change_id,
                        changeset_id,
                        file_change.scope,
                        file_change.remote_path,
                        file_change.expected_sha256,
                        file_change.replacement_sha256,
                        file_change.diff_text,
                        json.dumps(file_change.original_metadata),
                        file_change.local_backup_ref,
                        file_change.remote_backup_path,
                        file_change.temp_path,
                        int(file_change.applied),
                        file_change.validation_result,
                        file_change.rollback_result,
                    ),
                )
            conn.commit()

        return changeset

    def update_file_change(
        self,
        changeset_id: str,
        file_change_id: str,
        *,
        local_backup_ref: str | None = None,
        remote_backup_path: str | None = None,
        temp_path: str | None = None,
        applied: bool | None = None,
        validation_result: str | None = None,
        rollback_result: str | None = None,
    ) -> FileChange:
        """Update mutable fields on a single file change record."""
        updates: list[str] = []
        params: list[object] = []
        for column, value in (
            ("local_backup_ref", local_backup_ref),
            ("remote_backup_path", remote_backup_path),
            ("temp_path", temp_path),
            ("applied", applied),
            ("validation_result", validation_result),
            ("rollback_result", rollback_result),
        ):
            if value is not None:
                updates.append(f"{column} = ?")
                params.append(int(value) if column == "applied" else value)

        if not updates:
            raise ValueError("no fields to update")

        params.extend([changeset_id, file_change_id])
        with self._connection_factory() as conn:
            cursor = conn.execute(
                f"UPDATE file_changes SET {', '.join(updates)} "
                "WHERE changeset_id = ? AND file_change_id = ?",
                params,
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise ChangeSetNotFound(
                    f"file change '{file_change_id}' not found in '{changeset_id}'"
                )

        changeset = self.get(changeset_id)
        if changeset is None:
            raise ChangeSetNotFound(f"ChangeSet '{changeset_id}' not found")
        for file_change in changeset.files:
            if file_change.file_change_id == file_change_id:
                return file_change
        raise ChangeSetNotFound(
            f"file change '{file_change_id}' not found in '{changeset_id}'"
        )

    def get(self, changeset_id: str) -> ChangeSet | None:
        """Retrieve a ChangeSet by ID, including its file changes."""
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                SELECT changeset_id, incident_id, project_id, target_id, service_name,
                       status, verification_plan, rollback_plan, approval_id,
                       created_at, updated_at
                FROM changesets WHERE changeset_id = ?
                """,
                (changeset_id,),
            )
            row = cursor.fetchone()

            if row is None:
                return None

            file_cursor = conn.execute(
                """
                SELECT file_change_id, scope, remote_path, expected_sha256,
                       replacement_sha256, diff_text, original_metadata,
                       local_backup_ref, remote_backup_path, temp_path,
                       applied, validation_result, rollback_result
                FROM file_changes WHERE changeset_id = ?
                """,
                (changeset_id,),
            )
            file_rows = file_cursor.fetchall()

        files = tuple(
            FileChange(
                file_change_id=fr[0],
                scope=fr[1],
                remote_path=fr[2],
                expected_sha256=fr[3],
                replacement_sha256=fr[4],
                diff_text=fr[5],
                original_metadata=json.loads(fr[6]),
                local_backup_ref=fr[7],
                remote_backup_path=fr[8],
                temp_path=fr[9],
                applied=bool(fr[10]),
                validation_result=fr[11],
                rollback_result=fr[12],
            )
            for fr in file_rows
        )

        return ChangeSet(
            changeset_id=row[0],
            incident_id=row[1],
            project_id=row[2],
            target_id=row[3],
            service_name=row[4],
            status=ChangeSetStatus(row[5]),
            files=files,
            verification_plan=row[6],
            rollback_plan=row[7],
            approval_id=row[8],
            created_at=datetime.fromisoformat(row[9]),
            updated_at=datetime.fromisoformat(row[10]),
        )

    def list_for_incident(self, incident_id: str, limit: int = 100) -> list[ChangeSet]:
        """Return changesets for an incident, newest first."""
        with self._connection_factory() as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT changeset_id FROM changesets "
                    "WHERE incident_id = ? ORDER BY created_at DESC LIMIT ?",
                    (incident_id, limit),
                ).fetchall()
            ]
        changesets: list[ChangeSet] = []
        for changeset_id in ids:
            changeset = self.get(changeset_id)
            if changeset is not None:
                changesets.append(changeset)
        return changesets

    def transition(self, changeset_id: str, target_status: ChangeSetStatus) -> ChangeSet:
        """Transition a ChangeSet to the target status.

        Raises ChangeSetNotFound if the changeset does not exist.
        Raises InvalidChangeTransition if the transition is not allowed.
        """
        changeset = self.get(changeset_id)
        if changeset is None:
            raise ChangeSetNotFound(f"ChangeSet '{changeset_id}' not found")

        current_status = changeset.status

        # Terminal states accept no further transitions
        if current_status in _TERMINAL_STATES:
            raise InvalidChangeTransition(
                f"Cannot transition from terminal status '{current_status.value}'"
            )

        # Check if target status is reachable from current status
        allowed_predecessors = _VALID_PREDECESSORS.get(target_status)
        if allowed_predecessors is None or current_status not in allowed_predecessors:
            raise InvalidChangeTransition(
                f"Cannot transition from '{current_status.value}' to '{target_status.value}'"
            )

        now = datetime.now(UTC)

        with self._connection_factory() as conn:
            conn.execute(
                """
                UPDATE changesets
                SET status = ?, updated_at = ?
                WHERE changeset_id = ?
                """,
                (target_status.value, now.isoformat(), changeset_id),
            )
            conn.commit()

        return self.get(changeset_id)  # type: ignore[return-value]
