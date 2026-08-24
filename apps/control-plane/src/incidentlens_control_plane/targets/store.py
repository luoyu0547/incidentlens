"""SQLite persistence for the target facade bindings.

Follows the runtime.db / sqlite3 conventions of the sibling stores (projects,
approvals, evidence): an idempotent ``migrate()`` and validated Pydantic round
trips.  The table carries only facade identity plus product metadata — the
authoritative host/user/port/services/scope live in ``projects.record_json``.

``update`` applies a conditional UPDATE on ``version`` so a stale facade write
surfaces as :class:`TargetVersionConflict` instead of a silent lost update.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime

from incidentlens_control_plane.targets.types import TargetBinding


class TargetAlreadyExists(Exception):
    """Raised when creating a binding whose target_id already exists."""


class TargetNotFound(Exception):
    """Raised when a requested facade target has no binding."""


class TargetVersionConflict(Exception):
    """Raised when a facade write targets a stale ``version``."""


_TARGET_BINDING_COLUMNS = (
    "target_id",
    "project_id",
    "registry_target_id",
    "name",
    "authentication_ref",
    "host_key_policy",
    "pinned_host_key_sha256",
    "version",
    "created_at",
    "updated_at",
)


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _iso(value: datetime) -> str:
    return value.isoformat()


class TargetStore:
    """SQLite-backed store for ``target_facade_bindings`` rows."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connection_factory = connection_factory

    def migrate(self) -> None:
        """Create the target facade bindings table if it doesn't exist."""
        with self._connection_factory() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS target_facade_bindings (
                    target_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    registry_target_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    authentication_ref TEXT NOT NULL,
                    host_key_policy TEXT NOT NULL
                        CHECK (host_key_policy IN ('strict', 'pinned')),
                    pinned_host_key_sha256 TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (project_id, registry_target_id)
                )
                """
            )
            conn.commit()

    def get(self, target_id: str) -> TargetBinding:
        """Return one facade binding, or raise TargetNotFound."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_TARGET_BINDING_COLUMNS)}
                FROM target_facade_bindings WHERE target_id = ?
                """,
                (target_id,),
            ).fetchone()
        if row is None:
            raise TargetNotFound(f"target '{target_id}' not found")
        return self._row_to_binding(row)

    def list(self) -> tuple[TargetBinding, ...]:
        """Return all facade bindings, ordered by target_id."""
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_TARGET_BINDING_COLUMNS)}
                FROM target_facade_bindings ORDER BY target_id
                """
            ).fetchall()
        return tuple(self._row_to_binding(row) for row in rows)

    def create(self, binding: TargetBinding) -> TargetBinding:
        """Persist a new facade binding; raise TargetAlreadyExists on a duplicate."""
        with self._connection_factory() as conn:
            try:
                conn.execute(
                    f"""
                    INSERT INTO target_facade_bindings (
                        {", ".join(_TARGET_BINDING_COLUMNS)}
                    ) VALUES ({_placeholders(len(_TARGET_BINDING_COLUMNS))})
                    """,
                    self._binding_to_row(binding),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise TargetAlreadyExists(
                    f"target '{binding.target_id}' already exists"
                ) from exc
        return binding

    def update(
        self, binding: TargetBinding, *, expected_version: int
    ) -> TargetBinding:
        """Replace a binding conditional on its current ``version``.

        Raises ``TargetVersionConflict`` when *expected_version* no longer
        matches the stored version and ``TargetNotFound`` when the row is gone.
        """
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                UPDATE target_facade_bindings
                SET project_id = ?, registry_target_id = ?, name = ?,
                    authentication_ref = ?, host_key_policy = ?,
                    pinned_host_key_sha256 = ?, version = ?, updated_at = ?
                WHERE target_id = ? AND version = ?
                """,
                (
                    binding.project_id,
                    binding.registry_target_id,
                    binding.name,
                    binding.authentication_ref,
                    binding.host_key_policy,
                    binding.pinned_host_key_sha256,
                    binding.version,
                    _iso(binding.updated_at),
                    binding.target_id,
                    expected_version,
                ),
            )
            conn.commit()
            if cursor.rowcount == 0:
                exists = conn.execute(
                    "SELECT 1 FROM target_facade_bindings WHERE target_id = ?",
                    (binding.target_id,),
                ).fetchone()
                if exists is None:
                    raise TargetNotFound(
                        f"target '{binding.target_id}' not found"
                    )
                raise TargetVersionConflict(
                    f"target '{binding.target_id}' was modified concurrently"
                )
        return binding

    def delete(self, target_id: str) -> None:
        """Delete a facade binding; raise TargetNotFound if missing."""
        with self._connection_factory() as conn:
            cursor = conn.execute(
                "DELETE FROM target_facade_bindings WHERE target_id = ?",
                (target_id,),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise TargetNotFound(f"target '{target_id}' not found")

    def _binding_to_row(self, binding: TargetBinding) -> tuple[object, ...]:
        return (
            binding.target_id,
            binding.project_id,
            binding.registry_target_id,
            binding.name,
            binding.authentication_ref,
            binding.host_key_policy,
            binding.pinned_host_key_sha256,
            binding.version,
            _iso(binding.created_at),
            _iso(binding.updated_at),
        )

    def _row_to_binding(self, row: tuple[object, ...]) -> TargetBinding:
        return TargetBinding(
            target_id=str(row[0]),
            project_id=str(row[1]),
            registry_target_id=str(row[2]),
            name=str(row[3]),
            authentication_ref=str(row[4]),
            host_key_policy=row[5],  # type: ignore[arg-type]
            pinned_host_key_sha256=(
                str(row[6]) if row[6] is not None else None
            ),
            version=int(row[7]),
            created_at=datetime.fromisoformat(str(row[8])),
            updated_at=datetime.fromisoformat(str(row[9])),
        )
