"""Dream consolidation for project memory.

Implements periodic consolidation of memory entries with locking,
staging, and atomic replacement.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from incidentlens_control_plane.project_memory.domain import MemoryCandidate
from incidentlens_control_plane.project_memory.store import MEMORY_DIR, ProjectMemoryStore


@dataclass
class DreamDecision:
    """Decision from DreamGate.evaluate()."""

    should_run: bool
    reason: str = ""


@dataclass
class DreamGate:
    """Controls when Dream consolidation should run.

    Requirements:
    - 24 hours since last successful run
    - 5 completed Agent turns
    - Scan throttling
    - Project lock that expires after 1 hour
    """

    min_hours_since_last: float = 24.0
    min_turns: int = 5
    lock_expiry_seconds: int = 3600  # 1 hour

    _last_run_time: float = field(default_factory=float)
    _completed_turns: int = field(default_factory=int)
    _lock_path: Path | None = field(default=None)
    _lock_acquired_at: float = field(default_factory=float)

    def record_successful_run(self) -> None:
        """Record a successful Dream run."""
        self._last_run_time = time.time()

    def record_turn_complete(self) -> None:
        """Record a completed Agent turn."""
        self._completed_turns += 1

    def evaluate(
        self,
        now: float | None = None,
        turn_count: int | None = None,
        lock: Path | None = None,
    ) -> DreamDecision:
        """Evaluate whether Dream should run.

        Parameters
        ----------
        now:
            Current timestamp (defaults to time.time()).
        turn_count:
            Current turn count (defaults to internal counter).
        lock:
            Path to lock file.

        Returns
        -------
        DreamDecision
            Decision with should_run flag and reason.
        """
        now = now if now is not None else time.time()
        turn_count = turn_count if turn_count is not None else self._completed_turns
        lock = lock or self._lock_path

        # Check 24 hours since last run
        hours_since_last = (now - self._last_run_time) / 3600
        if hours_since_last < self.min_hours_since_last:
            return DreamDecision(
                should_run=False,
                reason=f"only {hours_since_last:.1f} hours since last run, need {self.min_hours_since_last}",
            )

        # Check minimum turns
        if turn_count < self.min_turns:
            return DreamDecision(
                should_run=False,
                reason=f"only {turn_count} completed turns, need {self.min_turns}",
            )

        # Check lock expiry
        if lock and lock.exists():
            try:
                lock_mtime = lock.stat().st_mtime
                lock_age = now - lock_mtime
                if lock_age < self.lock_expiry_seconds:
                    return DreamDecision(
                        should_run=False,
                        reason=f"lock is {lock_age:.0f}s old, expires in {self.lock_expiry_seconds - lock_age:.0f}s",
                    )
            except OSError:
                pass

        return DreamDecision(should_run=True, reason="all conditions met")


@dataclass
class DreamTransaction:
    """Manages Dream consolidation transactions.

    Ensures atomic replacement of memory files with staging.
    """

    def __init__(self, base_dir: Path, gate: DreamGate | None = None) -> None:
        self._base_dir = base_dir
        self._store = ProjectMemoryStore(base_dir)
        self._gate = gate or DreamGate()
        self._lock_path: Path | None = None

    async def execute(
        self,
        candidates: list[MemoryCandidate],
        model: Any,
        now: float | None = None,
        turn_count: int | None = None,
    ) -> dict[str, Any]:
        """Execute a Dream consolidation transaction.

        Parameters
        ----------
        candidates:
            Consolidated memory candidates to apply.
        model:
            Language model for validation.
        now:
            Current timestamp (for testing).
        turn_count:
            Current turn count (for testing).

        Returns
        -------
        dict
            Result with status, files modified, etc.
        """
        # Check gate
        decision = self._gate.evaluate(now=now, turn_count=turn_count)
        if not decision.should_run:
            return {"status": "skipped", "reason": decision.reason}

        # Acquire lock
        lock_path = self._base_dir / MEMORY_DIR / ".consolidate-lock"
        if not self._acquire_lock(lock_path):
            return {"status": "skipped", "reason": "could not acquire lock"}

        try:
            # Stage directory for atomic writes
            staging_dir = self._base_dir / MEMORY_DIR / ".staging"
            staging_dir.mkdir(parents=True, exist_ok=True)

            # Write candidates to staging
            staged_files: list[Path] = []
            for candidate in candidates:
                staging_path = staging_dir / f"{candidate.name}.md"

                # Build frontmatter
                now = datetime.now(tz=timezone.utc)
                frontmatter = yaml.safe_dump(
                    {
                        "name": candidate.name,
                        "type": candidate.type.value,
                        "description": candidate.description,
                        "created_at": now.isoformat(),
                    },
                    default_flow_style=False,
                    sort_keys=False,
                ).strip()

                content = f"---\n{frontmatter}\n---\n\n{candidate.body}\n"
                staging_path.write_text(content, encoding="utf-8")

                # fsync
                fd = os.open(str(staging_path), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

                staged_files.append(staging_path)

            # Validate staged files
            for staged in staged_files:
                if not staged.is_file():
                    self._cleanup_staging(staging_dir, staged_files)
                    return {"status": "failed", "reason": f"staged file missing: {staged}"}

            # Replace individual target files
            target_dir = self._base_dir / MEMORY_DIR
            for staged in staged_files:
                target = target_dir / staged.name
                shutil.move(str(staged), str(target))

            # Cleanup staging directory
            shutil.rmtree(staging_dir, ignore_errors=True)

            # Record successful run
            self._gate.record_successful_run()

            return {
                "status": "success",
                "files_modified": len(candidates),
                "candidates": [c.name for c in candidates],
            }

        except Exception as e:
            # On failure, leave original files and index unchanged
            self._cleanup_staging(staging_dir, staged_files)
            return {"status": "failed", "reason": str(e)}

        finally:
            self._release_lock(lock_path)

    def _acquire_lock(self, lock_path: Path) -> bool:
        """Acquire exclusive lock with creation."""
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            # Use exclusive creation (fail if exists)
            fd = os.open(
                str(lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self._lock_path = lock_path
            return True
        except FileExistsError:
            return False

    def _release_lock(self, lock_path: Path) -> None:
        """Release lock file."""
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._lock_path = None

    def _cleanup_staging(self, staging_dir: Path, staged_files: list[Path]) -> None:
        """Clean up staging directory."""
        for f in staged_files:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            staging_dir.rmdir()
        except OSError:
            pass
