"""Opt-in live acceptance test against a disposable Docker SSH target.

This test is DISABLED by default.  It exercises the real ``AsyncSshTransport``
against an ephemeral OpenSSH container (``infra/test-ssh``) and verifies the
eight Phase 2 acceptance points:

1. Session reuse: two ``SessionManager.connect()`` calls share one session.
2. Persistent shell state: ``cd /workspace/service`` affects a later ``pwd``.
3. Read/list/stat return bounded results through ``RemoteToolGateway``.
4. A large multi-location edit creates an encrypted local and a timestamped
   remote backup before replacing the file.
5. A stale expected hash blocks a second edit.
6. ``rm -rf`` is permanently rejected without contacting the transport.
7. ``docker restart`` produces an approval request and is not executed first.
8. A forced validation failure leaves the original bytes untouched.

Run it only when Docker is available and you explicitly opt in::

    INCIDENTLENS_RUN_LIVE_SSH=1 uv run pytest tests/integration/test_live_ssh_tools.py -q

The fixture skips with a clear reason when the opt-in variable is absent, when
the Docker CLI/daemon or ``ssh-keygen`` is unavailable, or when the SSH target
never becomes reachable.  The private key is generated fresh under ``tmp_path``
and is never committed; the compose project is torn down with
``docker compose down --volumes`` in a finalizer.
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
from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.changes.backup import BackupReference, EncryptedBackupVault
from incidentlens_control_plane.changes.manager import ChangeManager
from incidentlens_control_plane.changes.store import ChangeSetStore
from incidentlens_control_plane.changes.types import ChangeSetStatus
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.project_registry.types import (
    ProjectRecord,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.asyncssh_adapter import AsyncSshTransportFactory
from incidentlens_control_plane.remote_ops.gateway import (
    CommandForbidden,
    Gateway,
    RemoteToolGateway,
)
from incidentlens_control_plane.remote_ops.policy import CommandPolicy
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.shell import PersistentShell
from incidentlens_control_plane.remote_ops.types import (
    DockerActionKind,
    DockerActionRequest,
    HostScope,
    OperationRisk,
    ShellRequest,
    TextReplacement,
)

PROJECT_NAME = "incidentlens-live"
PROJECT_ID = "liveproj"
TARGET_ID = "live-ssh"
SERVICE = "test-ssh"
WORKSPACE = PurePosixPath("/workspace/service")


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


async def _wait_for_ssh(factory: AsyncSshTransportFactory, target: TargetRegistration) -> None:
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
    if os.environ.get("INCIDENTLENS_RUN_LIVE_SSH") != "1":
        pytest.skip("INCIDENTLENS_RUN_LIVE_SSH=1 is not set; the live SSH test is opt-in")

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker executable not found on PATH; cannot run the live SSH test")
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        pytest.skip("ssh-keygen executable not found on PATH; cannot run the live SSH test")

    daemon = subprocess.run([docker, "info"], capture_output=True, text=True, timeout=30)
    if daemon.returncode != 0:
        pytest.skip("docker daemon is not running; cannot start the disposable SSH target")

    tmp_path = tmp_path_factory.mktemp("live-ssh")
    compose_file = Path(__file__).resolve().parents[2] / "infra" / "test-ssh" / "compose.yaml"
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
        allowed_host_paths=(WORKSPACE,),
        allowed_container_paths=(PurePosixPath("/app"),),
    )
    project_record = ProjectRecord(
        project_id=PROJECT_ID,
        display_name="Live SSH Acceptance",
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
# Harness: in-memory project registry + real ChangeManager/approvals
# ---------------------------------------------------------------------------


class _InMemoryProjects:
    """Minimal ``ProjectRegistryStore``-compatible lookup for the fixture."""

    def __init__(self, record: ProjectRecord) -> None:
        self._record = record

    def get(self, project_id: str) -> ProjectRecord:
        if project_id != self._record.project_id:
            raise KeyError(f"project {project_id!r} not found")
        return self._record


def _make_sqlite_connect(tmp_path: Path, name: str):
    db_path = tmp_path / name

    def connect():
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    return connect


def _make_approval_service(tmp_path: Path) -> ApprovalService:
    connect = _make_sqlite_connect(tmp_path, "approvals.db")
    approvals = ApprovalStore(connect)
    events = RuntimeEventStore(connect)
    broker = RuntimeEventBroker()
    approvals.migrate()
    events.migrate()
    return ApprovalService(approvals=approvals, events=events, broker=broker)


@dataclass
class _Harness:
    gateway: RemoteToolGateway
    sessions: SessionManager
    approvals: ApprovalService
    changes: ChangeManager
    store: ChangeSetStore
    vault: EncryptedBackupVault
    vault_root: Path


def _build_harness(tmp_path: Path, live: LiveTarget) -> _Harness:
    """Build an isolated gateway with fresh stores for one test."""
    projects = _InMemoryProjects(live.project_record)
    targets = {live.target.target_id: live.target}
    sessions = SessionManager(live.factory)
    approvals = _make_approval_service(tmp_path)

    store = ChangeSetStore(_make_sqlite_connect(tmp_path, "changes.db"))
    store.migrate()
    vault_root = tmp_path / "backups"
    vault = EncryptedBackupVault(vault_root, tmp_path / "key.bin")

    changes = ChangeManager(
        store=store,
        vault=vault,
        projects=projects,
        sessions=sessions,
        targets=targets,
        approvals=approvals,
    )
    gateway = RemoteToolGateway(
        projects=projects,
        sessions=sessions,
        targets=targets,
        changes=changes,
        approvals=approvals,
    )
    return _Harness(
        gateway=gateway,
        sessions=sessions,
        approvals=approvals,
        changes=changes,
        store=store,
        vault=vault,
        vault_root=vault_root,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Acceptance point 1: one persistent connection is reused
# ---------------------------------------------------------------------------


def test_two_connect_calls_reuse_one_session_id(live_target: LiveTarget) -> None:
    async def scenario() -> None:
        sessions = SessionManager(live_target.factory)
        first = await sessions.connect(live_target.target)
        second = await sessions.connect(live_target.target)
        assert first.session_id == second.session_id
        await sessions.close_all()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Acceptance point 2: persistent shell state (cd) survives across commands
# ---------------------------------------------------------------------------


def test_persistent_shell_cd_affects_following_pwd(live_target: LiveTarget) -> None:
    async def scenario() -> None:
        sessions = SessionManager(live_target.factory)
        session = await sessions.connect(live_target.target)
        process = await session.transport.open_shell()
        shell = PersistentShell(process)
        try:
            cd = await shell.execute("cd /workspace/service", timeout=15.0)
            assert cd.exit_status == 0, cd
            pwd = await shell.execute("pwd", timeout=15.0)
            assert pwd.exit_status == 0, pwd
            assert pwd.stdout.rstrip() == b"/workspace/service"
        finally:
            await shell.close()
            await sessions.close_all()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Acceptance point 3: read / list / stat return bounded results
# ---------------------------------------------------------------------------


def test_read_list_stat_return_bounded_results(live_target: LiveTarget, tmp_path: Path) -> None:
    harness = _build_harness(tmp_path, live_target)
    path = WORKSPACE / "readme_live.py"
    content = b"import os\n\nprint('hello live world')\n" * 40

    async def scenario() -> None:
        write_result = await harness.gateway.write(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=path,
            content=content,
        )
        assert write_result.status is ChangeSetStatus.APPLIED, write_result

        result = await harness.gateway.read(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=path,
        )
        assert result.content == content
        assert result.sha256 == _sha256(content)
        assert result.truncated is False

        listing = await harness.gateway.list_dir(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=WORKSPACE,
        )
        names = {entry.path.name for entry in listing}
        assert "readme_live.py" in names

        meta = await harness.gateway.stat(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=path,
        )
        assert meta.size == len(content)
        assert meta.is_symlink is False

        matches = await harness.gateway.search(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=WORKSPACE,
            query="hello live world",
        )
        assert any(match.path == path for match in matches)

        await harness.sessions.close_all()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Acceptance point 4: large multi-location edit creates both backups first
# ---------------------------------------------------------------------------


def _large_py_file() -> bytes:
    lines = [
        "# Live integration service file.",
        'VERSION = "1.0"',
        "",
        "def handler(request):",
        '    name = request.get("name", "world")',
        '    return {"greeting": f"hello {name}"}',
        "",
        "def main():",
        '    print(handler({"name": "live"}))',
        "",
        'if __name__ == "__main__":',
        "    main()",
    ]
    lines.append("")
    lines.extend(f"MARKER_{i} = {i}" for i in range(80))
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_multi_location_edit_creates_encrypted_local_and_remote_backups(
    live_target: LiveTarget, tmp_path: Path
) -> None:
    harness = _build_harness(tmp_path, live_target)
    path = WORKSPACE / "service_live.py"
    original = _large_py_file()
    replacements = (
        TextReplacement(old_text='VERSION = "1.0"', new_text='VERSION = "1.1"'),
        TextReplacement(old_text='"world"', new_text='"universe"'),
        TextReplacement(old_text="MARKER_7 = 7", new_text="MARKER_7 = 70"),
        TextReplacement(old_text="hello {name}", new_text="hey {name}"),
    )

    async def scenario() -> None:
        write_result = await harness.gateway.write(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=path,
            content=original,
        )
        assert write_result.status is ChangeSetStatus.APPLIED, write_result

        result = await harness.gateway.edit(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=path,
            expected_sha256=_sha256(original),
            replacements=replacements,
            incident_id="inc-edit-live",
        )
        assert result.status is ChangeSetStatus.APPLIED, result

        # The remote file now contains every replacement.
        current = await harness.gateway.read(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=path,
        )
        text = current.content.decode("utf-8")
        assert 'VERSION = "1.1"' in text
        assert '"universe"' in text
        assert "MARKER_7 = 70" in text
        assert "hey {name}" in text

        # An encrypted local backup exists and decrypts to the original bytes.
        enc_files = list(harness.vault_root.rglob("*.enc"))
        assert enc_files, "no encrypted local backup was created"
        backup_ref = BackupReference(
            local_path=enc_files[0], sha256=_sha256(original)
        )
        assert harness.vault.load(backup_ref) == original

        # A timestamped same-directory remote backup exists and equals the
        # original bytes.
        changeset = harness.store.get(result.changeset_id)
        assert changeset is not None
        file_change = changeset.files[0]
        assert file_change.local_backup_ref is not None
        remote_backup = PurePosixPath(file_change.remote_backup_path)
        assert ".incidentlens-backup." in remote_backup.name
        assert remote_backup.parent == path.parent
        backup_result = await harness.gateway.read(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=remote_backup,
        )
        assert backup_result.content == original

        await harness.sessions.close_all()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Acceptance point 5: a stale expected hash blocks a second edit
# ---------------------------------------------------------------------------


def test_stale_hash_blocks_second_edit(live_target: LiveTarget, tmp_path: Path) -> None:
    harness = _build_harness(tmp_path, live_target)
    path = WORKSPACE / "stale_live.py"
    original = b"value = 1\n"

    async def scenario() -> None:
        write_result = await harness.gateway.write(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=path,
            content=original,
        )
        assert write_result.status is ChangeSetStatus.APPLIED, write_result

        first = await harness.gateway.edit(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=path,
            expected_sha256=_sha256(original),
            replacements=(TextReplacement(old_text="1", new_text="2"),),
        )
        assert first.status is ChangeSetStatus.APPLIED, first

        # Reusing the now-stale original hash must fail without a write.
        second = await harness.gateway.edit(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=path,
            expected_sha256=_sha256(original),
            replacements=(TextReplacement(old_text="2", new_text="3"),),
        )
        assert second.status is ChangeSetStatus.FAILED
        assert second.error is not None

        current = await harness.gateway.read(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=path,
        )
        assert current.content == b"value = 2\n"

        await harness.sessions.close_all()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Acceptance point 6: rm -rf is rejected without contacting the transport
# ---------------------------------------------------------------------------


def test_recursive_force_rm_rejected_without_transport(
    live_target: LiveTarget, tmp_path: Path
) -> None:
    harness = _build_harness(tmp_path, live_target)

    async def scenario() -> None:
        request = ShellRequest(
            operation_id="op-rm",
            incident_id="inc-rm",
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            scope=HostScope(),
            command="rm -rf /workspace/service",
            reason="attempt a destructive recursive force delete",
        )
        decision = CommandPolicy().evaluate(request, live_target.service)
        assert decision.risk is OperationRisk.FORBIDDEN
        assert decision.approval_can_override is False

        # No session exists before the attempt; the gateway rejects a FORBIDDEN
        # command before any approval or transport contact.
        assert await harness.sessions.find_live(TARGET_ID) is None
        with pytest.raises(CommandForbidden):
            await Gateway(approvals=harness.approvals).shell(
                request.model_copy(update={"risk": decision.risk})
            )
        assert harness.approvals.list() == ()
        assert await harness.sessions.find_live(TARGET_ID) is None

        await harness.sessions.close_all()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Acceptance point 7: docker restart is gated behind exact approval
# ---------------------------------------------------------------------------


def test_docker_restart_requires_approval_and_is_not_executed_first(
    live_target: LiveTarget, tmp_path: Path
) -> None:
    harness = _build_harness(tmp_path, live_target)

    def container_started_at() -> str:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.StartedAt}}",
                live_target.container_name,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    async def scenario() -> None:
        before = container_started_at()
        request = DockerActionRequest(
            operation_id="op-dk",
            incident_id="inc-dk",
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            scope=HostScope(),
            action=DockerActionKind.RESTART,
            container=live_target.container_name,
            reason="restart the disposable test ssh service",
        )

        result = await harness.gateway.docker_action(request)
        assert result.approved is False
        assert result.approval_id is not None
        assert result.exit_status is None

        # No transport contact happened: the container was never restarted.
        assert container_started_at() == before

        # Exactly one pending approval was created for the exact intent.
        pending = harness.approvals.list()
        assert len(pending) == 1
        assert pending[0].intent["kind"] == "docker_action"
        assert pending[0].intent["container"] == live_target.container_name

        await harness.sessions.close_all()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Acceptance point 8: a forced validation failure restores original bytes
# ---------------------------------------------------------------------------


def test_validation_failure_restores_original_bytes(
    live_target: LiveTarget, tmp_path: Path
) -> None:
    harness = _build_harness(tmp_path, live_target)
    path = WORKSPACE / "syntax_live.py"
    original = b"def ok():\n    return 1\n\nprint(ok())\n"

    async def scenario() -> None:
        write_result = await harness.gateway.write(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=path,
            content=original,
        )
        assert write_result.status is ChangeSetStatus.APPLIED, write_result

        # Replacing a structural element produces invalid Python; ast.parse
        # must reject it before any backup or write.
        result = await harness.gateway.edit(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=path,
            expected_sha256=_sha256(original),
            replacements=(TextReplacement(old_text="def ok():", new_text="def ok("),),
        )
        assert result.status is ChangeSetStatus.FAILED
        assert result.error is not None

        current = await harness.gateway.read(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            path=path,
        )
        assert current.content == original

        await harness.sessions.close_all()

    asyncio.run(scenario())
