"""Project memory runtime orchestration.

Provides bounded task supervision and per-turn memory management.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine

from incidentlens_control_plane.project_memory.domain import MemoryQuery
from incidentlens_control_plane.project_memory.extractor import extract_memories
from incidentlens_control_plane.project_memory.selector import select_memories
from incidentlens_control_plane.project_memory.store import ProjectMemoryStore


@dataclass
class MemoryTask:
    """A memory processing task."""

    incident_id: str
    turn_id: str
    task_type: str  # "extraction" | "dream"
    coroutine: Callable[[], Coroutine[Any, Any, Any]]
    priority: int = 0  # Higher = more priority

    def __lt__(self, other: "MemoryTask") -> bool:
        """Comparison for PriorityQueue (higher priority first)."""
        return self.priority > other.priority


class MemoryTaskSupervisor:
    """Bounded task supervisor for memory operations.

    Features:
    - asyncio.Queue with explicit task priority
    - Named worker tasks
    - Track tasks in set, consume exceptions
    - Stop submission idempotent by (incident_id, turn_id)
    """

    def __init__(self, max_workers: int = 2, max_queue_size: int = 10) -> None:
        self._max_workers = max_workers
        self._max_queue_size = max_queue_size
        self._queue: asyncio.PriorityQueue[MemoryTask] = asyncio.PriorityQueue(
            maxsize=max_queue_size
        )
        self._workers: list[asyncio.Task[None]] = []
        self._active_tasks: set[str] = set()  # "incident_id:turn_id"
        self._closed = False
        self._close_event = asyncio.Event()

    def start(self) -> None:
        """Start worker tasks."""
        if self._workers:
            return  # Already started

        for i in range(self._max_workers):
            worker = asyncio.create_task(
                self._worker(f"memory-worker-{i}"),
                name=f"memory-worker-{i}",
            )
            self._workers.append(worker)

    async def _worker(self, name: str) -> None:
        """Worker coroutine that processes tasks from the queue."""
        while not self._closed:
            try:
                task = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except RuntimeError:
                # Event loop closed
                break

            try:
                await task.coroutine()
            except Exception as exc:
                # Consume exception (log or handle as needed)
                print(f"[{name}] task failed: {exc}")
            finally:
                try:
                    self._queue.task_done()
                except RuntimeError:
                    # Event loop closed
                    pass

    def submit(self, task: MemoryTask) -> bool:
        """Submit a task to the queue.

        Returns False if:
        - Queue is full
        - Duplicate (incident_id, turn_id) already pending
        - Supervisor is closed
        """
        if self._closed:
            return False

        task_key = f"{task.incident_id}:{task.turn_id}"

        # Idempotent check
        if task_key in self._active_tasks:
            return False

        try:
            self._queue.put_nowait(task)
            self._active_tasks.add(task_key)
            return True
        except asyncio.QueueFull:
            return False

    async def close(self, timeout_seconds: float = 5.0) -> None:
        """Close the supervisor and wait for workers to finish.

        Parameters
        ----------
        timeout_seconds:
            Maximum time to wait for workers to complete.
        """
        self._closed = True

        # Wait for queue to drain (with timeout)
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            pass

        # Cancel workers
        for worker in self._workers:
            worker.cancel()

        # Wait for workers to finish
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._active_tasks.clear()
        self._close_event.set()

    @property
    def is_closed(self) -> bool:
        """Check if supervisor is closed."""
        return self._closed

    @property
    def pending_count(self) -> int:
        """Number of pending tasks in queue."""
        return self._queue.qsize()

    @property
    def active_count(self) -> int:
        """Number of active task keys."""
        return len(self._active_tasks)


@dataclass
class ProjectMemoryRuntime:
    """Per-turn orchestration for project memory.

    Coordinates prefetching and extraction at turn boundaries.
    """

    base_dir: Path
    supervisor: MemoryTaskSupervisor = field(default_factory=MemoryTaskSupervisor)

    _prefetched_memories: dict[str, Any] = field(default_factory=dict)

    def start(self) -> None:
        """Start the runtime and task supervisor."""
        self.supervisor.start()

    async def on_turn_start(
        self,
        incident_id: str,
        turn_id: str,
        catalog: list[dict[str, Any]],
        model: Any,
    ) -> dict[str, Any]:
        """Prefetch memory at turn start.

        Parameters
        ----------
        incident_id:
            Current incident identifier.
        turn_id:
            Current turn identifier.
        catalog:
            Current memory catalog.
        model:
            Language model for selection.

        Returns
        -------
        dict
            Prefetched memory context.
        """
        # Simple query based on incident context
        query = MemoryQuery(
            alert_summary=f"Incident {incident_id}, Turn {turn_id}",
            recent_text="",
        )

        selection = await select_memories(query, catalog, model, limit=5)

        # Store for turn
        key = f"{incident_id}:{turn_id}"
        self._prefetched_memories[key] = selection

        return {
            "incident_id": incident_id,
            "turn_id": turn_id,
            "selection": selection.model_dump(),
        }

    async def on_turn_stop(
        self,
        incident_id: str,
        turn_id: str,
        transcript_path: str | Path,
        catalog: list[dict[str, Any]],
        model: Any,
    ) -> dict[str, Any]:
        """Submit extraction task at turn stop.

        Parameters
        ----------
        incident_id:
            Current incident identifier.
        turn_id:
            Current turn identifier.
        transcript_path:
            Path to bounded transcript file.
        catalog:
            Current memory catalog.
        model:
            Language model for extraction.

        Returns
        -------
        dict
            Submission status.
        """
        async def extraction_task() -> None:
            candidates = await extract_memories(transcript_path, catalog, model)
            if candidates:
                store = ProjectMemoryStore(self.base_dir)
                for candidate in candidates:
                    store.write(candidate)

        task = MemoryTask(
            incident_id=incident_id,
            turn_id=turn_id,
            task_type="extraction",
            coroutine=extraction_task,
            priority=1,  # Normal priority
        )

        submitted = self.supervisor.submit(task)

        # Cleanup prefetched memories
        key = f"{incident_id}:{turn_id}"
        self._prefetched_memories.pop(key, None)

        return {
            "incident_id": incident_id,
            "turn_id": turn_id,
            "submitted": submitted,
            "pending": self.supervisor.pending_count,
        }

    async def close(self, timeout_seconds: float = 5.0) -> None:
        """Close the runtime and task supervisor."""
        await self.supervisor.close(timeout_seconds)
