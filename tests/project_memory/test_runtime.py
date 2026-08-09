"""Tests for project_memory runtime."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from incidentlens_control_plane.project_memory.domain import MemoryCatalogEntry, MemoryType
from incidentlens_control_plane.project_memory.runtime import (
    MemoryTask,
    MemoryTaskSupervisor,
    ProjectMemoryRuntime,
)


# ---------------------------------------------------------------------------
# MemoryTaskSupervisor tests
# ---------------------------------------------------------------------------


class TestMemoryTaskSupervisor:
    """Tests for MemoryTaskSupervisor."""

    @pytest.mark.asyncio
    async def test_start_creates_workers(self) -> None:
        """start() creates worker tasks."""
        supervisor = MemoryTaskSupervisor(max_workers=2)
        supervisor.start()

        assert len(supervisor._workers) == 2
        for worker in supervisor._workers:
            assert not worker.done()

        await supervisor.close()

    @pytest.mark.asyncio
    async def test_submit_returns_true(self) -> None:
        """submit() returns True when task is accepted."""
        supervisor = MemoryTaskSupervisor()
        supervisor.start()

        async def dummy_coro() -> None:
            pass

        task = MemoryTask(
            incident_id="inc-1",
            turn_id="turn-1",
            task_type="extraction",
            coroutine=dummy_coro,
        )

        result = supervisor.submit(task)
        assert result is True

        await supervisor.close()

    @pytest.mark.asyncio
    async def test_submit_duplicate_returns_false(self) -> None:
        """submit() returns False for duplicate (incident_id, turn_id)."""
        supervisor = MemoryTaskSupervisor()
        supervisor.start()

        async def dummy_coro() -> None:
            pass

        task1 = MemoryTask(
            incident_id="inc-1",
            turn_id="turn-1",
            task_type="extraction",
            coroutine=dummy_coro,
        )
        task2 = MemoryTask(
            incident_id="inc-1",
            turn_id="turn-1",  # Same turn
            task_type="dream",
            coroutine=dummy_coro,
        )

        assert supervisor.submit(task1) is True
        assert supervisor.submit(task2) is False

        await supervisor.close()

    @pytest.mark.asyncio
    async def test_submit_after_close_returns_false(self) -> None:
        """submit() returns False after close()."""
        supervisor = MemoryTaskSupervisor()
        supervisor.start()
        await supervisor.close()

        async def dummy_coro() -> None:
            pass

        task = MemoryTask(
            incident_id="inc-1",
            turn_id="turn-1",
            task_type="extraction",
            coroutine=dummy_coro,
        )

        assert supervisor.submit(task) is False

    @pytest.mark.asyncio
    async def test_close_waits_with_timeout(self) -> None:
        """close() respects timeout."""
        supervisor = MemoryTaskSupervisor()
        supervisor.start()

        async def slow_task() -> None:
            await asyncio.sleep(10)

        task = MemoryTask(
            incident_id="inc-1",
            turn_id="turn-1",
            task_type="extraction",
            coroutine=slow_task,
        )
        supervisor.submit(task)

        # close with short timeout should complete quickly
        await supervisor.close(timeout_seconds=0.1)
        assert supervisor.is_closed

    @pytest.mark.asyncio
    async def test_workers_consume_exceptions(self) -> None:
        """Workers consume exceptions without crashing."""
        supervisor = MemoryTaskSupervisor()
        supervisor.start()

        call_count = 0

        async def failing_task() -> None:
            nonlocal call_count
            call_count += 1
            raise ValueError("Task failed")

        async def success_task() -> None:
            pass

        task1 = MemoryTask(
            incident_id="inc-1",
            turn_id="turn-1",
            task_type="extraction",
            coroutine=failing_task,
        )
        task2 = MemoryTask(
            incident_id="inc-2",
            turn_id="turn-2",
            task_type="extraction",
            coroutine=success_task,
        )

        supervisor.submit(task1)
        supervisor.submit(task2)

        await asyncio.sleep(0.1)
        await supervisor.close()

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_pending_count(self) -> None:
        """pending_count tracks queue size."""
        supervisor = MemoryTaskSupervisor()
        supervisor.start()

        async def slow_task() -> None:
            await asyncio.sleep(10)

        task1 = MemoryTask(
            incident_id="inc-1",
            turn_id="turn-1",
            task_type="extraction",
            coroutine=slow_task,
        )
        task2 = MemoryTask(
            incident_id="inc-2",
            turn_id="turn-2",
            task_type="extraction",
            coroutine=slow_task,
        )

        supervisor.submit(task1)
        supervisor.submit(task2)

        assert supervisor.pending_count == 2

        await supervisor.close()

    @pytest.mark.asyncio
    async def test_active_count(self) -> None:
        """active_count tracks submitted tasks."""
        supervisor = MemoryTaskSupervisor()
        supervisor.start()

        async def dummy_coro() -> None:
            pass

        task1 = MemoryTask(
            incident_id="inc-1",
            turn_id="turn-1",
            task_type="extraction",
            coroutine=dummy_coro,
        )
        task2 = MemoryTask(
            incident_id="inc-2",
            turn_id="turn-2",
            task_type="extraction",
            coroutine=dummy_coro,
        )

        supervisor.submit(task1)
        supervisor.submit(task2)

        assert supervisor.active_count == 2

        await supervisor.close()


# ---------------------------------------------------------------------------
# ProjectMemoryRuntime tests
# ---------------------------------------------------------------------------


class TestProjectMemoryRuntime:
    """Tests for ProjectMemoryRuntime."""

    @pytest.mark.asyncio
    async def test_on_turn_stop_returns_before_blocked_extractor(
        self, tmp_path: Path
    ) -> None:
        """on_turn_stop() returns immediately, not waiting for extraction."""
        runtime = ProjectMemoryRuntime(base_dir=tmp_path)
        runtime.start()

        # Track if extraction started
        extraction_started = False
        extraction_complete = False

        async def mock_extraction() -> None:
            nonlocal extraction_started, extraction_complete
            extraction_started = True
            await asyncio.sleep(10)  # Simulate slow extraction
            extraction_complete = True

        # Create mock catalog and model
        catalog = []
        model = AsyncMock()

        # Write a mock transcript
        transcript = tmp_path / "transcript.txt"
        transcript.write_text("Test transcript", encoding="utf-8")

        # Override extract_memories to use our mock
        with patch(
            "incidentlens_control_plane.project_memory.runtime.extract_memories",
            new=mock_extraction,
        ):
            result = await runtime.on_turn_stop(
                incident_id="inc-1",
                turn_id="turn-1",
                transcript_path=transcript,
                catalog=catalog,
                model=model,
            )

        # Should return immediately
        assert result["submitted"] is True
        assert not extraction_complete

        await runtime.close()

    @pytest.mark.asyncio
    async def test_duplicate_turn_ids_enqueue_once(self, tmp_path: Path) -> None:
        """Duplicate turn IDs are only enqueued once."""
        runtime = ProjectMemoryRuntime(base_dir=tmp_path)
        runtime.start()

        async def dummy_coro() -> None:
            pass

        with patch(
            "incidentlens_control_plane.project_memory.runtime.extract_memories"
        ):
            result1 = await runtime.on_turn_stop(
                incident_id="inc-1",
                turn_id="turn-1",
                transcript_path=tmp_path / "transcript.txt",
                catalog=[],
                model=AsyncMock(),
            )

            # Write transcript for second call
            (tmp_path / "transcript.txt").write_text("Test", encoding="utf-8")

            result2 = await runtime.on_turn_stop(
                incident_id="inc-1",
                turn_id="turn-1",  # Same turn
                transcript_path=tmp_path / "transcript.txt",
                catalog=[],
                model=AsyncMock(),
            )

        assert result1["submitted"] is True
        assert result2["submitted"] is False  # Duplicate rejected

        await runtime.close()

    @pytest.mark.asyncio
    async def test_dream_dropped_when_queue_full(self, tmp_path: Path) -> None:
        """Dream task is dropped when queue is full."""
        # Create supervisor with small queue
        supervisor = MemoryTaskSupervisor(max_workers=1, max_queue_size=2)
        runtime = ProjectMemoryRuntime(base_dir=tmp_path, supervisor=supervisor)
        runtime.start()

        async def blocking_task() -> None:
            await asyncio.sleep(10)

        # Fill the queue with blocking tasks
        for i in range(2):
            task = MemoryTask(
                incident_id=f"inc-{i}",
                turn_id=f"turn-{i}",
                task_type="extraction",
                coroutine=blocking_task,
            )
            supervisor.submit(task)

        # Now try to submit a dream task
        dream_task = MemoryTask(
            incident_id="dream",
            turn_id="turn-0",
            task_type="dream",
            coroutine=blocking_task,
            priority=0,  # Lower priority
        )

        result = supervisor.submit(dream_task)
        assert result is False  # Queue full, dream dropped

        await runtime.close()

    @pytest.mark.asyncio
    async def test_close_waits_only_up_to_timeout(self, tmp_path: Path) -> None:
        """close() respects timeout limit."""
        runtime = ProjectMemoryRuntime(base_dir=tmp_path)
        runtime.start()

        async def slow_task() -> None:
            await asyncio.sleep(10)

        # Submit a slow task
        task = MemoryTask(
            incident_id="inc-1",
            turn_id="turn-1",
            task_type="extraction",
            coroutine=slow_task,
        )
        runtime.supervisor.submit(task)

        # close with short timeout
        import time

        start = time.time()
        await runtime.close(timeout_seconds=0.1)
        elapsed = time.time() - start

        # Should complete in roughly the timeout period
        assert elapsed < 2.0

    @pytest.mark.asyncio
    async def test_on_turn_start_prefetches(self, tmp_path: Path) -> None:
        """on_turn_start() prefetches memory selection."""
        runtime = ProjectMemoryRuntime(base_dir=tmp_path)
        runtime.start()

        catalog = [
            MemoryCatalogEntry(
                name="test-memory",
                type=MemoryType.PROJECT,
                description="Test memory",
            )
        ]
        model = AsyncMock()

        # Mock select_memories to return a selection
        mock_selection = MagicMock()
        mock_selection.model_dump.return_value = {
            "filenames": ["test-memory"],
            "mode": "keyword",
            "reason": "test",
        }

        # Patch the selector module's select_memories function
        with patch(
            "incidentlens_control_plane.project_memory.selector.select_memories",
            new_callable=AsyncMock,
            return_value=mock_selection,
        ):
            result = await runtime.on_turn_start(
                incident_id="inc-1",
                turn_id="turn-1",
                catalog=catalog,
                model=model,
            )

        assert result["incident_id"] == "inc-1"
        assert result["turn_id"] == "turn-1"
        assert "selection" in result

        await runtime.close()
