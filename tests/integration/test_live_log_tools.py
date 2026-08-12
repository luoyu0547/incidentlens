"""Opt-in live acceptance test for the Phase 3 log investigation pipeline.

This test is DISABLED by default.  It exercises the real ``AsyncSshTransport``
against an ephemeral OpenSSH container (``infra/test-ssh``) and verifies the
Phase 3 acceptance points end to end:

1. Host file log query returns redacted-only content (a seeded token never
   survives into a ``LogRecord``).
2. An opt-in file subscription streams a newly appended line and persists a
   redacted record.
3. Restarting the app with the same ``data_dir`` resumes the subscription from
   its stored cursor and does not duplicate the replay-boundary line.
4. Evidence is created only from redacted content (its hash covers the redacted
   message).
5. Container list/search and docker log queries run against the registered
   container when docker is available on the target.

Run it only when Docker is available and you explicitly opt in::

    INCIDENTLENS_RUN_LIVE_LOG_TESTS=1 uv run pytest tests/integration/test_live_log_tools.py -q

The fixture skips with a clear reason when the opt-in variable is absent, when
the Docker CLI/daemon or ``ssh-keygen`` is unavailable, or when the SSH target
never becomes reachable.  The private key is generated fresh under ``tmp_path``
and is never committed; the compose project is torn down with
``docker compose down --volumes`` in a finalizer.

The docker acceptance points additionally require a ``docker`` CLI inside the
target container (the stock ``infra/test-ssh`` image installs only OpenSSH, so
against that image the docker sub-checks skip with a clear reason AFTER the
host-file/subscription/restart/evidence checks have run and passed).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.logs.service import LogService
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.logs.subscriptions import LogSubscriptionManager
from incidentlens_control_plane.logs.types import (
    LogQueryRequest,
    LogRecord,
    LogScope,
    LogSourceKind,
)
from incidentlens_control_plane.project_registry.types import (
    ProjectRecord,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.asyncssh_adapter import (
    AsyncSshTransportFactory,
)
from incidentlens_control_plane.remote_ops.sessions import SessionManager

PROJECT_NAME = "incidentlens-live-log"
PROJECT_ID = "livelogproj"
TARGET_ID = "live-log-ssh"
SERVICE = "test-ssh"
WORKSPACE = PurePosixPath("/workspace/service")
LOG_FILE = WORKSPACE / "live.log"

# A secret-looking token seeded into the live log file.  The redaction pipeline
# must ensure it never survives into a stored ``LogRecord``, an evidence ref, or
# a runtime event payload.
_LIVE_TOKEN = "super-secret-live"
_SEED_LINES = f"INFO payment start\nWARN token={_LIVE_TOKEN}\n"


# ---------------------------------------------------------------------------
# Fixture: disposable OpenSSH container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveTarget:
    """Everything a live test needs to connect to the disposable target."""

    target: TargetRegistration
    service: ServiceRegistration
    project_record: ProjectRecord
    factory: AsyncSshTransportFactory
    container_name: str


async def _wait_for_ssh(
    factory: AsyncSshTransportFactory, target: TargetRegistration
) -> None:
    """Poll until the SSH target accepts a real connection (host keys included)."""
    deadline = time.monotonic() + 90.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            transport = await asyncio.wait_for(factory.connect(target), timeout=10.0)
            await transport.close()
            return
        except Exception as exc:  # noqa: BLE001 - readiness probe must retry any failure
            last_error = exc
            await asyncio.sleep(0.5)
    raise RuntimeError(f"SSH target did not become ready: {last_error}")


def _wait_for_host_key(docker: str, container_name: str, timeout: float = 60.0) -> str:
    """Return the container's ed25519 host key once ``ssh-keygen -A`` has run."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                docker,
                "exec",
                container_name,
                "cat",
                "/etc/ssh/ssh_host_ed25519_key.pub",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        time.sleep(0.5)
    raise RuntimeError("container SSH host key was not generated in time")


@pytest.fixture(scope="module")
def live_target(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> LiveTarget:
    """Start the disposable OpenSSH container and return connection details.

    Skips when the opt-in variable is absent or Docker/ssh-keygen is
    unavailable.  Generates a fresh ed25519 keypair under tmp_path and tears
    the compose project down (with volumes) in a finalizer.
    """
    if os.environ.get("INCIDENTLENS_RUN_LIVE_LOG_TESTS") != "1":
        pytest.skip(
            "INCIDENTLENS_RUN_LIVE_LOG_TESTS=1 is not set; the live log test is opt-in"
        )

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker executable not found on PATH; cannot run the live log test")
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        pytest.skip("ssh-keygen executable not found on PATH; cannot run the live log test")

    daemon = subprocess.run([docker, "info"], capture_output=True, text=True, timeout=30)
    if daemon.returncode != 0:
        pytest.skip("docker daemon is not running; cannot start the disposable SSH target")

    tmp_path = tmp_path_factory.mktemp("live-log")
    compose_file = (
        Path(__file__).resolve().parents[2] / "infra" / "test-ssh" / "compose.yaml"
    )
    key_path = tmp_path / "id_ed25519"

    # Generate a fresh private/public keypair.  The private key stays under
    # tmp_path and is never committed or copied anywhere else.
    subprocess.run(
        [ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
        check=True,
    )
    env = {**os.environ, "TEST_AUTHORIZED_KEYS": str(key_path.with_suffix(".pub"))}

    def cleanup() -> None:
        subprocess.run(
            [
                docker,
                "compose",
                "-f",
                str(compose_file),
                "-p",
                PROJECT_NAME,
                "down",
                "--volumes",
            ],
            check=False,
            env=env,
        )

    request.addfinalizer(cleanup)

    subprocess.run(
        [docker, "compose", "-f", str(compose_file), "-p", PROJECT_NAME, "up", "-d", "--build"],
        check=True,
        env=env,
    )

    port_result = subprocess.run(
        [docker, "compose", "-f", str(compose_file), "-p", PROJECT_NAME, "port", SERVICE, "22"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    host_port = int(port_result.stdout.strip().rsplit(":", 1)[-1])

    ps_result = subprocess.run(
        [docker, "compose", "-f", str(compose_file), "-p", PROJECT_NAME, "ps", "-q", SERVICE],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    container_id = ps_result.stdout.strip()
    name_result = subprocess.run(
        [docker, "inspect", "-f", "{{.Name}}", container_id],
        capture_output=True,
        text=True,
        check=True,
    )
    container_name = name_result.stdout.strip().lstrip("/")

    # Capture the container's ephemeral host key (generated by ``ssh-keygen -A``
    # on startup) and trust exactly that key through a test-only known_hosts
    # file.  Production targets keep the default behavior (verify against the
    # user's known_hosts).
    host_key = _wait_for_host_key(docker, container_name)
    known_hosts_path = tmp_path / "known_hosts"
    known_hosts_path.write_text(f"[127.0.0.1]:{host_port} {host_key}\n")

    target = TargetRegistration(
        target_id=TARGET_ID,
        host="127.0.0.1",
        ssh_user="incidentlens",
        port=host_port,
    )
    service = ServiceRegistration(
        compose_service=SERVICE,
        container_names=(container_name,),
        allowed_log_paths=(str(WORKSPACE),),
        allowed_host_paths=(WORKSPACE,),
        allowed_container_paths=(PurePosixPath("/app"),),
    )
    project_record = ProjectRecord(
        project_id=PROJECT_ID,
        display_name="Live Log Acceptance",
        targets=(target,),
        services=(service,),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    factory = AsyncSshTransportFactory(
        client_key_paths=(str(key_path),),
        known_hosts_path=str(known_hosts_path),
    )

    asyncio.run(_wait_for_ssh(factory, target))

    return LiveTarget(
        target=target,
        service=service,
        project_record=project_record,
        factory=factory,
        container_name=container_name,
    )


# ---------------------------------------------------------------------------
# Harness: in-memory project registry + real log stores over a temp SQLite db
# ---------------------------------------------------------------------------


class _InMemoryProjects:
    """Minimal ``ProjectRegistryStore``-compatible lookup for the fixture."""

    def __init__(self, record: ProjectRecord) -> None:
        self._record = record

    def get(self, project_id: str) -> ProjectRecord:
        if project_id != self._record.project_id:
            raise KeyError(f"project {project_id!r} not found")
        return self._record


def _make_sqlite_connect(db_path: Path):
    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    return connect


@dataclass
class _LogHarness:
    service: LogService
    store: LogStore
    evidence: EvidenceStore
    subscriptions: LogSubscriptionManager
    sessions: SessionManager


def _build_log_harness(tmp_path: Path, live: LiveTarget) -> _LogHarness:
    """Build an isolated log pipeline over one persistent SQLite database.

    ``db_path`` lives under ``tmp_path`` so a second harness built from the
    same path (the restart step) reads the same subscriptions, records, and
    cursors.
    """
    projects = _InMemoryProjects(live.project_record)
    sessions = SessionManager(live.factory)
    db_path = tmp_path / "runtime.db"
    connect = _make_sqlite_connect(db_path)
    store = LogStore(connect)
    store.migrate()
    evidence = EvidenceStore(connect)
    evidence.migrate()
    events = RuntimeEventStore(connect)
    events.migrate()
    broker = RuntimeEventBroker()
    service = LogService(
        projects=projects,
        store=store,
        sessions=sessions,
        evidence=evidence,
    )
    settings = RuntimeSettings(
        data_dir=tmp_path / "data",
        log_file_poll_interval_seconds=0.1,
    )
    subscriptions = LogSubscriptionManager(
        store=store,
        service=service,
        events=events,
        broker=broker,
        settings=settings,
    )
    return _LogHarness(
        service=service,
        store=store,
        evidence=evidence,
        subscriptions=subscriptions,
        sessions=sessions,
    )


async def _write_remote_log(live: LiveTarget, content: bytes) -> None:
    """(Re)write the live log file over SFTP on a fresh connection."""
    sessions = SessionManager(live.factory)
    try:
        session = await sessions.connect(live.target)
        await session.transport.write_bytes(LOG_FILE, content, mode=0o644)
    finally:
        await sessions.close_all()


async def _append_remote_log(live: LiveTarget, text: str) -> None:
    """Append one line to the live log file via a remote shell append."""
    sessions = SessionManager(live.factory)
    try:
        session = await sessions.connect(live.target)
        result = await session.transport.run_argv(
            ("sh", "-c", f'printf "%s\\n" \'{text}\' >> {LOG_FILE}'),
            timeout=15.0,
        )
        assert result.exit_status == 0, result
    finally:
        await sessions.close_all()


async def _await_records(
    store: LogStore, subscription_id: str, expected: int
) -> tuple[LogRecord, ...]:
    """Wait until the subscription has at least ``expected`` persisted records."""
    for _ in range(600):
        records = store.list_records_for_subscription(subscription_id, limit=1000)
        if len(records) >= expected:
            return records
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"subscription {subscription_id} never reached {expected} records "
        f"(last count {len(records)})"
    )


# ---------------------------------------------------------------------------
# Acceptance: container list/search, file redaction, docker logs, subscription,
# restart-resume dedupe, and redacted-only evidence
# ---------------------------------------------------------------------------


def test_live_container_list_search_file_docker_stream_restart_dedupe_and_evidence(
    live_target: LiveTarget, tmp_path: Path
) -> None:
    harness = _build_log_harness(tmp_path, live_target)

    async def scenario() -> None:
        # 1. On-demand host file query redacts the seeded token.
        await _write_remote_log(live_target, _SEED_LINES.encode("utf-8"))
        query = await harness.service.query(
            LogQueryRequest(
                project_id=PROJECT_ID,
                target_id=TARGET_ID,
                service_name=SERVICE,
                source_kind=LogSourceKind.FILE,
                scope=LogScope.HOST,
                source_ref=str(LOG_FILE),
                tail_lines=100,
                persist=False,
            ),
            now=datetime.now(UTC),
        )
        assert len(query) == 2, query
        assert all(_LIVE_TOKEN not in record.message_redacted for record in query)
        assert any("[REDACTED_TOKEN]" in record.message_redacted for record in query)

        # 2. Opt-in file subscription persists the seed lines, then streams a
        #    newly appended line.
        subscription = await harness.subscriptions.create(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service_name=SERVICE,
            source_kind=LogSourceKind.FILE,
            scope=LogScope.HOST,
            source_ref=str(LOG_FILE),
            opt_in_streaming=True,
            created_by="live-test",
        )
        await _await_records(harness.store, subscription.subscription_id, 2)
        await _append_remote_log(live_target, "INFO live appended line")
        first_records = await _await_records(
            harness.store, subscription.subscription_id, 3
        )
        assert any(
            "live appended line" in record.message_redacted for record in first_records
        )
        assert all(_LIVE_TOKEN not in record.message_redacted for record in first_records)

        # 3. Restart with the same data dir: a fresh manager over the same
        #    SQLite store restores the active subscription from its stored
        #    cursor and picks up a line appended after restart without
        #    duplicating the replay-boundary line.
        await harness.subscriptions.close_all()
        restarted = _build_log_harness(tmp_path, live_target)
        await restarted.subscriptions.start_active_opt_in()
        await _append_remote_log(live_target, "WARN live restart line")
        final_records = await _await_records(
            restarted.store, subscription.subscription_id, 4
        )

        assert len(final_records) == 4
        assert len({record.dedupe_key for record in final_records}) == 4
        messages = [record.message_redacted for record in final_records]
        assert messages.count("WARN live restart line") == 1
        assert messages.count("INFO live appended line") == 1
        assert all(_LIVE_TOKEN not in record.message_redacted for record in final_records)

        # 4. Evidence is created only from redacted content.
        token_record = next(
            record
            for record in final_records
            if "[REDACTED_TOKEN]" in record.message_redacted
        )
        ref = restarted.evidence.create_from_log_record(
            token_record,
            incident_id="inc-live-log",
            created_by="live-test",
            now=datetime.now(UTC),
        )
        assert ref.content_redacted == token_record.message_redacted
        assert _LIVE_TOKEN not in ref.content_redacted
        assert ref.content_sha256 == hashlib.sha256(
            token_record.message_redacted.encode("utf-8")
        ).hexdigest()

        # 5. Container list/search and docker logs require a docker CLI on the
        #    target.  The stock infra/test-ssh image installs only OpenSSH, so
        #    these sub-checks skip with a clear reason after every
        #    docker-independent check above has run and passed.
        session = await restarted.sessions.connect(live_target.target)
        docker_probe = await session.transport.run_argv(
            ("sh", "-c", "command -v docker"), timeout=15.0
        )
        if docker_probe.exit_status != 0:
            pytest.skip(
                "docker CLI is not available on the live SSH target; "
                "host-file/subscription/restart/evidence checks already passed, "
                "docker checks require docker available on the target"
            )

        listing = await session.transport.run_argv(
            ("docker", "ps", "--format", "{{.Names}}"), timeout=30.0
        )
        assert listing.exit_status == 0, listing
        assert live_target.container_name in listing.stdout.decode("utf-8")

        filtered = await session.transport.run_argv(
            (
                "docker",
                "ps",
                "--format",
                "{{.Names}}",
                "--filter",
                f"name={live_target.container_name}",
            ),
            timeout=30.0,
        )
        assert filtered.exit_status == 0, filtered
        assert live_target.container_name in filtered.stdout.decode("utf-8")

        docker_records = await harness.service.query(
            LogQueryRequest(
                project_id=PROJECT_ID,
                target_id=TARGET_ID,
                service_name=SERVICE,
                source_kind=LogSourceKind.DOCKER,
                scope=LogScope.CONTAINER,
                source_ref=live_target.container_name,
                tail_lines=50,
                persist=False,
            ),
            now=datetime.now(UTC),
        )
        assert docker_records
        assert all(
            _LIVE_TOKEN not in record.message_redacted for record in docker_records
        )

        await restarted.subscriptions.close_all()
        await restarted.sessions.close_all()
        await harness.sessions.close_all()

    asyncio.run(scenario())
