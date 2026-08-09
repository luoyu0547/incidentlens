"""Tests for project_memory dream consolidation."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from incidentlens_control_plane.project_memory.dream import (
    DreamDecision,
    DreamGate,
    DreamTransaction,
)
from incidentlens_control_plane.project_memory.domain import MemoryCandidate, MemoryType


# ---------------------------------------------------------------------------
# DreamGate.evaluate tests
# ---------------------------------------------------------------------------


class TestDreamGateEvaluate:
    """Tests for DreamGate.evaluate() skip reasons."""

    def test_skip_hours_since_last_run(self) -> None:
        """Should skip if less than 24 hours since last run."""
        gate = DreamGate()
        gate._last_run_time = time.time() - 3600  # 1 hour ago

        decision = gate.evaluate(now=time.time(), turn_count=10)

        assert not decision.should_run
        assert "hours since last run" in decision.reason

    def test_skip_turn_count(self) -> None:
        """Should skip if fewer than 5 completed turns."""
        gate = DreamGate()
        gate._last_run_time = 0  # Long time ago

        decision = gate.evaluate(now=time.time(), turn_count=3)

        assert not decision.should_run
        assert "completed turns" in decision.reason

    def test_skip_lock_not_expired(self, tmp_path: Path) -> None:
        """Should skip if lock is less than 1 hour old."""
        gate = DreamGate()
        gate._last_run_time = 0  # Long time ago

        lock_path = tmp_path / ".lock"
        lock_path.write_text("123", encoding="utf-8")
        # Lock was just created

        decision = gate.evaluate(now=time.time(), turn_count=10, lock=lock_path)

        assert not decision.should_run
        assert "lock is" in decision.reason

    def test_lock_expired_allows_run(self, tmp_path: Path) -> None:
        """Should allow run if lock is older than 1 hour."""
        gate = DreamGate()
        gate._last_run_time = 0  # Long time ago

        lock_path = tmp_path / ".lock"
        lock_path.write_text("123", encoding="utf-8")
        # Make lock appear old by modifying mtime
        old_time = time.time() - 3700  # Just over 1 hour
        os.utime(lock_path, (old_time, old_time))

        decision = gate.evaluate(now=time.time(), turn_count=10, lock=lock_path)

        assert decision.should_run
        assert decision.reason == "all conditions met"

    def test_all_conditions_met(self) -> None:
        """Should allow run when all conditions are met."""
        gate = DreamGate()
        gate._last_run_time = 0  # Long time ago

        decision = gate.evaluate(now=time.time(), turn_count=10)

        assert decision.should_run
        assert decision.reason == "all conditions met"

    def test_skip_hours_not_met(self) -> None:
        """Should skip if exactly at 24 hours boundary."""
        gate = DreamGate()
        gate._last_run_time = time.time() - (24 * 3600 - 1)  # Just under 24 hours

        decision = gate.evaluate(now=time.time(), turn_count=10)

        assert not decision.should_run
        assert "hours since last run" in decision.reason


# ---------------------------------------------------------------------------
# DreamGate locking tests
# ---------------------------------------------------------------------------


class TestDreamGateLocking:
    """Tests for DreamGate lock expiry."""

    def test_lock_expiry_seconds(self) -> None:
        """Lock expiry defaults to 3600 seconds."""
        gate = DreamGate()
        assert gate.lock_expiry_seconds == 3600

    def test_lock_expiry_custom(self) -> None:
        """Lock expiry can be customized."""
        gate = DreamGate(lock_expiry_seconds=1800)
        assert gate.lock_expiry_seconds == 1800


# ---------------------------------------------------------------------------
# DreamTransaction tests
# ---------------------------------------------------------------------------


class TestDreamTransaction:
    """Tests for DreamTransaction execution."""

    @pytest.mark.asyncio
    async def test_execute_skipped_by_gate(self, tmp_path: Path) -> None:
        """Transaction skipped if gate says no."""
        gate = DreamGate()
        gate._last_run_time = time.time()  # Just ran

        transaction = DreamTransaction(tmp_path, gate=gate)

        result = await transaction.execute([], None)

        assert result["status"] == "skipped"
        assert "hours since last run" in result["reason"]

    @pytest.mark.asyncio
    async def test_execute_success(self, tmp_path: Path) -> None:
        """Successful transaction writes files."""
        gate = DreamGate()
        gate._last_run_time = 0  # Long time ago

        transaction = DreamTransaction(tmp_path, gate=gate)

        candidates = [
            MemoryCandidate(
                name="test-memory",
                description="Test memory",
                type=MemoryType.PROJECT,
                body="Test body content",
            )
        ]

        result = await transaction.execute(
            candidates, None, now=time.time(), turn_count=10
        )

        assert result["status"] == "success"
        assert result["files_modified"] == 1

        # Verify file was written
        memory_dir = tmp_path / ".incidentlens" / "memory"
        assert (memory_dir / "test-memory.md").exists()

    @pytest.mark.asyncio
    async def test_execute_cleanup_on_failure(self, tmp_path: Path) -> None:
        """Staging cleaned up on failure."""
        gate = DreamGate()
        gate._last_run_time = 0  # Long time ago

        transaction = DreamTransaction(tmp_path, gate=gate)

        # Create a candidate that will cause failure (name with path traversal after validation)
        # We'll create a valid candidate then corrupt the staging
        candidates = [
            MemoryCandidate(
                name="valid-name",
                description="Test",
                type=MemoryType.PROJECT,
                body="Test body",
            )
        ]

        # Mock _acquire_lock to succeed, but cause failure during move
        with patch.object(transaction, "_acquire_lock", return_value=True):
            with patch("shutil.move", side_effect=OSError("Permission denied")):
                result = await transaction.execute(
                    candidates, None, now=time.time(), turn_count=10
                )

        # Should fail but clean up
        assert result["status"] == "failed"

        # Staging directory should be cleaned up
        staging = tmp_path / ".incidentlens" / "memory" / ".staging"
        assert not staging.exists()


# ---------------------------------------------------------------------------
# Concurrent acquisition tests
# ---------------------------------------------------------------------------


class TestConcurrentAcquisition:
    """Tests for concurrent lock acquisition."""

    def test_exactly_one_winner(self, tmp_path: Path) -> None:
        """Only one transaction can acquire the lock."""
        gate = DreamGate()
        gate._last_run_time = 0

        transaction1 = DreamTransaction(tmp_path, gate=gate)
        transaction2 = DreamTransaction(tmp_path, gate=gate)

        lock_path = tmp_path / ".incidentlens" / "memory" / ".consolidate-lock"

        # First acquires lock
        assert transaction1._acquire_lock(lock_path)

        # Second fails
        assert not transaction2._acquire_lock(lock_path)

        # Release
        transaction1._release_lock(lock_path)

        # Now second can acquire
        assert transaction2._acquire_lock(lock_path)
        transaction2._release_lock(lock_path)

    def test_lock_file_created(self, tmp_path: Path) -> None:
        """Lock file is created on acquisition."""
        gate = DreamGate()
        transaction = DreamTransaction(tmp_path, gate=gate)
        lock_path = tmp_path / ".incidentlens" / "memory" / ".consolidate-lock"

        assert transaction._acquire_lock(lock_path)
        assert lock_path.exists()

        transaction._release_lock(lock_path)
        assert not lock_path.exists()

    def test_lock_contains_pid(self, tmp_path: Path) -> None:
        """Lock file contains the process ID."""
        gate = DreamGate()
        transaction = DreamTransaction(tmp_path, gate=gate)
        lock_path = tmp_path / ".incidentlens" / "memory" / ".consolidate-lock"

        transaction._acquire_lock(lock_path)

        content = lock_path.read_text(encoding="utf-8")
        assert content == str(os.getpid())

        transaction._release_lock(lock_path)
