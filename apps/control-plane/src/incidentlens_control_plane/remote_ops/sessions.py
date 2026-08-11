"""Session management for persistent remote host and container connections."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from incidentlens_control_plane.project_registry.types import TargetRegistration
from incidentlens_control_plane.remote_ops.transport import (
    RemoteProcess,
    RemoteTransport,
    RemoteTransportFactory,
)


@dataclass
class HostSession:
    """A persistent connection to a single remote target."""

    session_id: str
    target_id: str
    transport: RemoteTransport
    connected_at: datetime
    host_process: RemoteProcess | None = None


@dataclass
class ContainerSession:
    """An independent child process running inside a container on a host."""

    session_id: str
    parent_session_id: str
    container: str
    process: RemoteProcess


class SessionManager:
    """Maintains one live ``HostSession`` per target and scoped container children."""

    def __init__(self, factory: RemoteTransportFactory) -> None:
        self._factory = factory
        self._sessions: dict[str, HostSession] = {}
        self._container_sessions: dict[str, ContainerSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, target_id: str) -> asyncio.Lock:
        if target_id not in self._locks:
            self._locks[target_id] = asyncio.Lock()
        return self._locks[target_id]

    async def connect(self, target: TargetRegistration) -> HostSession:
        """Return the existing live session or create a new one.

        If a session exists but its transport is dead, close stale resources
        and establish a fresh connection.
        """
        async with self._lock_for(target.target_id):
            existing = self._sessions.get(target.target_id)
            if existing is not None:
                if await existing.transport.is_alive():
                    return existing
                # Stale session -- close children and transport before reconnecting.
                await self._close_children_for_host(existing.session_id)
                await existing.transport.close()

            transport = await self._factory.connect(target)
            session = HostSession(
                session_id=uuid.uuid4().hex,
                target_id=target.target_id,
                transport=transport,
                connected_at=datetime.now(timezone.utc),
            )
            self._sessions[target.target_id] = session
            return session

    async def spawn_container_session(
        self, host_session_id: str, container: str
    ) -> ContainerSession:
        """Spawn a fresh container exec session as a child of a host session."""
        host = self._find_host_by_session_id(host_session_id)
        if host is None:
            raise ValueError(f"no host session with id {host_session_id!r}")

        proc = await host.transport.open_process(
            ("docker", "exec", "-i", container, "env", "PS1=", "sh"),
            term_type=None,
        )
        child = ContainerSession(
            session_id=uuid.uuid4().hex,
            parent_session_id=host_session_id,
            container=container,
            process=proc,
        )
        self._container_sessions[child.session_id] = child
        return child

    async def close_container_session(self, session_id: str) -> None:
        """Close a single container session without affecting its host."""
        child = self._container_sessions.pop(session_id, None)
        if child is not None:
            await child.process.close()

    async def disconnect(self, target_id: str) -> None:
        """Disconnect a target -- idempotent."""
        session = self._sessions.pop(target_id, None)
        if session is None:
            return
        await self._close_children_for_host(session.session_id)
        await session.transport.close()

    async def close_all(self) -> None:
        """Close every session -- idempotent."""
        for session in list(self._sessions.values()):
            await self._close_children_for_host(session.session_id)
            await session.transport.close()
        self._sessions.clear()
        self._container_sessions.clear()

    # --- internal helpers ---

    def _find_host_by_session_id(self, session_id: str) -> HostSession | None:
        for session in self._sessions.values():
            if session.session_id == session_id:
                return session
        return None

    async def _close_children_for_host(self, host_session_id: str) -> None:
        to_close = [
            sid
            for sid, child in self._container_sessions.items()
            if child.parent_session_id == host_session_id
        ]
        for sid in to_close:
            child = self._container_sessions.pop(sid)
            await child.process.close()
