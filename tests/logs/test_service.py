"""LogService pipeline tests."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.logs.service import LogService
from incidentlens_control_plane.logs.store import LogSearchFilters, LogStore
from incidentlens_control_plane.logs.types import (
    LogQueryRequest,
    LogScope,
    LogSourceKind,
)
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
from incidentlens_control_plane.remote_ops.sessions import SessionManager

PAYMENTS_NOW = datetime(2026, 8, 12, tzinfo=UTC)


def build_test_log_service(
    tmp_path: Path, target_registration: TargetRegistration
) -> LogService:
    """Build a LogService over a fresh runtime.db with the payments project."""
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
        now=PAYMENTS_NOW,
    )
    evidence = EvidenceStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    evidence.migrate()
    return LogService(
        projects=projects,
        store=store,
        sessions=SessionManager(FakeTransportFactory()),
        evidence=evidence,
    )


@pytest.mark.asyncio
async def test_log_service_query_redacts_before_persisting(
    tmp_path, target_registration
) -> None:
    service = build_test_log_service(tmp_path, target_registration)
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
        service._store.search(
            LogSearchFilters(project_id="payments", text="ERROR"), limit=10
        )
        == records
    )


@pytest.mark.asyncio
async def test_log_service_persist_repoll_returns_stored_records(
    tmp_path, target_registration
) -> None:
    service = build_test_log_service(tmp_path, target_registration)
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
    assert service._store.search(LogSearchFilters(project_id="payments"), limit=10) == second


@pytest.mark.asyncio
async def test_log_service_rejects_container_scope_file_reads(
    tmp_path, target_registration
) -> None:
    from incidentlens_control_plane.logs.sources import LogSourceUnavailable

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
        now=PAYMENTS_NOW,
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


@pytest.mark.asyncio
async def test_query_create_evidence_requires_incident_id(
    tmp_path, target_registration
) -> None:
    service = build_test_log_service(tmp_path, target_registration)

    with pytest.raises(ValueError, match="incident_id is required"):
        await service.query(
            LogQueryRequest(
                project_id="payments",
                target_id="dev-a",
                service_name="payment-api",
                source_kind=LogSourceKind.FILE,
                scope=LogScope.HOST,
                source_ref="/var/log/payment/app.log",
                persist=True,
                create_evidence=True,
                incident_id=None,
            ),
            now=datetime(2026, 8, 12, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_query_create_evidence_persists_and_propagates_ref_id(
    tmp_path, target_registration
) -> None:
    service = build_test_log_service(tmp_path, target_registration)
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
            create_evidence=True,
            incident_id="inc-1",
        ),
        now=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )

    assert len(records) == 1
    assert records[0].evidence_ref_id is not None
    assert records[0].evidence_ref_id.startswith("ev-")

    evidence = service._evidence.get(records[0].evidence_ref_id)
    assert evidence.incident_id == "inc-1"
    assert evidence.content_redacted == records[0].message_redacted
    assert "abc123" not in evidence.content_redacted
    assert service._evidence.list_for_incident("inc-1", limit=10) == (evidence,)


@pytest.mark.asyncio
async def test_flat_json_line_without_message_persists_no_secrets(
    tmp_path, target_registration
) -> None:
    """A flat JSON log line with secrets and NO message key must not persist them.

    The message falls back to the raw JSON text, which is redacted before
    persisting so neither the password nor the token survive into the stored
    record or the FTS index.
    """
    service = build_test_log_service(tmp_path, target_registration)
    session = await service._sessions.connect(target_registration)
    session.transport._files[PurePosixPath("/var/log/payment/app.log")] = (
        b'{"lvl":"ERROR","password":"hunter2","token":"abc123"}\n'
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
    assert "hunter2" not in records[0].message_redacted
    assert "abc123" not in records[0].message_redacted
    stored = service._store.search(
        LogSearchFilters(project_id="payments"), limit=10
    )
    assert len(stored) == 1
    assert "hunter2" not in stored[0].message_redacted
    assert "abc123" not in stored[0].message_redacted


@pytest.mark.asyncio
async def test_docker_reconnect_overlap_uses_stable_dedupe_identity(
    tmp_path, target_registration
) -> None:
    """Replayed docker lines across a reconnect must dedupe to one record.

    The docker cursor's per-stream ``seq`` restarts on every reconnect, so a
    replayed overlap line is renumbered.  Dedupe identity must be built from
    the timestamp+message instead of the cursor, so the replayed line produces
    the same ``dedupe_key`` as the original and only one record persists.
    """
    from incidentlens_control_plane.logs.types import RawLogLine

    from logs.conftest import docker_subscription

    service = build_test_log_service(tmp_path, target_registration)
    subscription = docker_subscription("payments-api-1")
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)

    original = RawLogLine(
        source_ref="payments-api-1",
        cursor="docker:time=2026-08-12T10:00:00Z:seq=5",
        observed_at=now,
        text="INFO once",
    )
    replayed = RawLogLine(
        source_ref="payments-api-1",
        cursor="docker:time=2026-08-12T10:00:00Z:seq=1",
        observed_at=now,
        text="INFO once",
    )

    records = service.process_raw_lines(
        (original, replayed), now=now, subscription=subscription
    )
    assert records[0].dedupe_key == records[1].dedupe_key

    inserted = service._store.append_batch(records)
    assert len(inserted) == 1
    stored = service._store.search(
        LogSearchFilters(project_id="payments"), limit=10
    )
    assert len(stored) == 1


@pytest.mark.asyncio
async def test_docker_query_persist_keeps_distinct_offset_lines(
    tmp_path, target_registration
) -> None:
    """Distinct docker query lines must not collapse into one dedupe_key.

    Query cursors are ``docker:<ref>:<offset>`` (not ``docker:time=...``), so
    the dedupe identity must fall back to the full cursor.  Two distinct lines
    with identical redacted messages persist as two records, while the replayed
    ``docker:time=`` stream line (round-1 test) still dedupes to one.
    """
    from incidentlens_control_plane.remote_ops.fakes import FakeChangeTransport

    service = build_test_log_service(tmp_path, target_registration)
    session = await service._sessions.connect(target_registration)
    session.transport = FakeChangeTransport()
    session.transport.docker_logs[("payments-api-1", 50)] = (
        b"2026-08-12T10:00:00Z ERROR same\n"
        b"2026-08-12T10:00:00Z ERROR same\n"
    )

    records = await service.query(
        LogQueryRequest(
            project_id="payments",
            target_id="dev-a",
            service_name="payment-api",
            source_kind=LogSourceKind.DOCKER,
            scope=LogScope.CONTAINER,
            source_ref="payments-api-1",
            tail_lines=50,
            persist=True,
            create_evidence=False,
        ),
        now=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )

    assert len(records) == 2
    assert len({record.dedupe_key for record in records}) == 2
    stored = service._store.search(
        LogSearchFilters(project_id="payments"), limit=10
    )
    assert len(stored) == 2

