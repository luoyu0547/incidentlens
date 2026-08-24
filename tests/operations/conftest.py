"""Shared fixtures for durable-operation task tests.

``runtime_factory`` builds a fresh :class:`RuntimeServices` over one shared
``runtime.db`` (so consecutive factories simulate a process restart against the
same durable state) with a fake transport so nothing touches the network.  It
returns a lightweight namespace exposing the attribute names the recovery /
dispatcher tests use: ``operations`` (the OperationService, with ``enqueue``),
``operation_store``, ``operation_recovery``, ``dispatcher`` and a
``changes`` double that records every ``rollback`` call for the no-replay
assertion without altering the production manager.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
from incidentlens_control_plane.runtime import build_runtime


class RecordingChanges:
    """Test double that delegates to ChangeManager while recording rollbacks.

    ``rollback_calls`` is a list of ``(changeset_id, approval_id)`` tuples so the
    recovery no-replay test can observe that NO rollback executed after a
    restart.  Every other attribute delegates to the real manager, so the
    dispatcher's registered rollback handler keeps working unchanged.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.rollback_calls: list[tuple[str, str | None]] = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def rollback(self, changeset_id: str, approval_id: str | None = None) -> None:
        self.rollback_calls.append((changeset_id, approval_id))
        await self._real.rollback(changeset_id, approval_id)


@pytest.fixture
def runtime_factory(tmp_path: Path):
    """Build independent runtimes over one shared data directory on demand."""
    data_dir = tmp_path / "data"

    def _factory() -> SimpleNamespace:
        runtime = build_runtime(
            RuntimeSettings(data_dir=data_dir),
            transport_factory=FakeTransportFactory(),
        )
        return SimpleNamespace(
            operations=runtime.operation_service,
            operation_store=runtime.operation_store,
            operation_recovery=runtime.operation_recovery,
            dispatcher=runtime.dispatcher,
            changes=RecordingChanges(runtime.changes),
            investigation_store=runtime.investigation_store,
            target_store=runtime.target_store,
            projects=runtime.projects,
            sessions=runtime.sessions,
            _runtime=runtime,
        )

    return _factory
