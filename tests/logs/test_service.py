"""LogService pipeline tests."""

import sqlite3
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest
from incidentlens_control_plane.logs.store import LogSearchFilters, LogStore
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
)


@pytest.mark.asyncio
async def test_log_service_query_redacts_before_persisting(
    tmp_path, target_registration
) -> None:
    from incidentlens_control_plane.logs.service import LogService
    from incidentlens_control_plane.logs.types import (
        LogQueryRequest,
        LogScope,
        LogSourceKind,
    )
    from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
    from incidentlens_control_plane.remote_ops.sessions import SessionManager

    store = LogStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    projects = ProjectRegistryStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    projects.migrate()
    projects.create(
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            targets=(target_registration,),
            services=(
                ServiceRegistration(
                    compose_service="payment-api",
                    container_names=("payments-api-1",),
                    allowed_log_paths=("/var/log/payment/app.log",),
                    allowed_host_paths=(PurePosixPath("/var/log/payment"),),
                ),
            ),
        ),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    service = LogService(
        projects=projects,
        store=store,
        sessions=SessionManager(FakeTransportFactory()),
    )
    session = await service._sessions.connect(target_registration)
    session.transport._files[PurePosixPath("/var/log/payment/app.log")] = (
        b"ERROR token=abc123\n"
    )

    records = await service.query(
        LogQueryRequest(
            project_id="payments",
            target_id="dev-a",
            service_name="payment-api",
            source_kind=LogSourceKind.FILE,
            scope=LogScope.HOST,
            source_ref="/var/log/payment/app.log",
            tail_lines=10,
            persist=True,
            create_evidence=False,
        ),
        now=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )

    assert len(records) == 1
    assert "abc123" not in records[0].message_redacted
    assert (
        store.search(LogSearchFilters(project_id="payments", text="ERROR"), limit=10)
        == records
    )


@pytest.mark.asyncio
async def test_log_service_persist_repoll_returns_stored_records(
    tmp_path, target_registration
) -> None:
    from incidentlens_control_plane.logs.service import LogService
    from incidentlens_control_plane.logs.types import (
        LogQueryRequest,
        LogScope,
        LogSourceKind,
    )
    from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
    from incidentlens_control_plane.remote_ops.sessions import SessionManager

    store = LogStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    projects = ProjectRegistryStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    projects.migrate()
    projects.create(
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            targets=(target_registration,),
            services=(
                ServiceRegistration(
                    compose_service="payment-api",
                    container_names=("payments-api-1",),
                    allowed_log_paths=("/var/log/payment/app.log",),
                    allowed_host_paths=(PurePosixPath("/var/log/payment"),),
                ),
            ),
        ),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    service = LogService(
        projects=projects,
        store=store,
        sessions=SessionManager(FakeTransportFactory()),
    )
    session = await service._sessions.connect(target_registration)
    session.transport._files[PurePosixPath("/var/log/payment/app.log")] = (
        b"ERROR token=abc123\n"
    )

    request = LogQueryRequest(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        tail_lines=10,
        persist=True,
        create_evidence=False,
    )

    first = await service.query(request, now=datetime(2026, 8, 12, 10, 0, tzinfo=UTC))
    second = await service.query(request, now=datetime(2026, 8, 12, 11, 0, tzinfo=UTC))

    assert len(second) == 1
    # A re-poll of unchanged content must not mint phantom log_ids.
    assert second[0].log_id == first[0].log_id
    assert store.search(LogSearchFilters(project_id="payments"), limit=10) == second


@pytest.mark.asyncio
async def test_log_service_rejects_container_scope_file_reads(
    tmp_path, target_registration
) -> None:
    from incidentlens_control_plane.logs.service import LogService
    from incidentlens_control_plane.logs.sources import LogSourceUnavailable
    from incidentlens_control_plane.logs.types import (
        LogQueryRequest,
        LogScope,
        LogSourceKind,
    )
    from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
    from incidentlens_control_plane.remote_ops.sessions import SessionManager

    store = LogStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    projects = ProjectRegistryStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    projects.migrate()
    projects.create(
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            targets=(target_registration,),
            services=(
                ServiceRegistration(
                    compose_service="payment-api",
                    container_names=("payments-api-1",),
                    allowed_log_paths=(),
                    allowed_container_paths=(PurePosixPath("/app/logs"),),
                ),
            ),
        ),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    service = LogService(
        projects=projects,
        store=store,
        sessions=SessionManager(FakeTransportFactory()),
    )

    with pytest.raises(LogSourceUnavailable):
        await service.query(
            LogQueryRequest(
                project_id="payments",
                target_id="dev-a",
                service_name="payment-api",
                source_kind=LogSourceKind.FILE,
                scope=LogScope.CONTAINER,
                source_ref="/app/logs/app.log",
                tail_lines=10,
                persist=False,
                create_evidence=False,
            ),
            now=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        )
