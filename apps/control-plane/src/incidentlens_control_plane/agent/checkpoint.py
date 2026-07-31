"""Async SQLite checkpoint runtime for LangGraph investigation graphs."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


class CheckpointCorruptError(Exception):
    """Raised when a checkpoint cannot be deserialized from the database."""

    def __init__(self, incident_id: str, detail: str = "") -> None:
        self.incident_id = incident_id
        self.detail = detail
        msg = f"Checkpoint corrupt for incident {incident_id}"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class AgentCheckpointRuntime:
    """Async context manager that owns an ``AsyncSqliteSaver`` backed by
    a SQLite file on disk.

    Usage::

        async with AgentCheckpointRuntime(db_path) as cp:
            graph = builder.compile(checkpointer=cp.saver)
            await graph.ainvoke(state, cp.config_for("inc-1"))

    The saver is closed automatically when the context manager exits.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        self.saver: AsyncSqliteSaver | None = None
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> AgentCheckpointRuntime:
        self._conn = aiosqlite.connect(self._db_path)
        await self._conn.__aenter__()
        self.saver = AsyncSqliteSaver(self._conn)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.saver = None
        if self._conn is not None:
            await self._conn.__aexit__(exc_type, exc_val, exc_tb)
            self._conn = None

    def config_for(self, incident_id: str) -> dict[str, dict[str, str]]:
        """Return a LangGraph config that maps ``thread_id`` to *incident_id*."""
        return {"configurable": {"thread_id": incident_id}}
