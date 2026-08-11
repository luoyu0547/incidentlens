"""Session-manager reuse and lifecycle tests."""

from __future__ import annotations

import asyncio

from incidentlens_control_plane.project_registry.types import TargetRegistration
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
from incidentlens_control_plane.remote_ops.sessions import SessionManager


def test_session_manager_reuses_one_transport_per_target(
    target_registration: TargetRegistration,
) -> None:
    async def scenario() -> None:
        factory = FakeTransportFactory()
        manager = SessionManager(factory)
        first = await manager.connect(target_registration)
        second = await manager.connect(target_registration)

        assert first is second
        assert factory.connect_calls == [target_registration]
        await manager.close_all()
        assert factory.transports[0].closed is True

    asyncio.run(scenario())


def test_dead_transport_is_reconnected_without_reusing_unknown_shell_state(
    target_registration: TargetRegistration,
) -> None:
    async def scenario() -> None:
        factory = FakeTransportFactory()
        manager = SessionManager(factory)
        first = await manager.connect(target_registration)
        first.transport.alive = False

        second = await manager.connect(target_registration)

        assert second.session_id != first.session_id
        assert second.host_process is None
        assert len(factory.connect_calls) == 2

    asyncio.run(scenario())


def test_container_session_is_an_independent_child(
    target_registration: TargetRegistration,
) -> None:
    async def scenario() -> None:
        manager = SessionManager(FakeTransportFactory())
        host = await manager.connect(target_registration)
        child = await manager.spawn_container_session(
            host.session_id, "payments-api-1"
        )

        assert child.session_id != host.session_id
        assert child.parent_session_id == host.session_id
        await manager.close_container_session(child.session_id)
        assert await host.transport.is_alive() is True

    asyncio.run(scenario())
