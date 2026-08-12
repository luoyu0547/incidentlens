"""Tests for the agent-safe tool registry and the evidence-first executor.

The harness wires a real SQLite-backed project registry, log store, evidence
store, investigation store, approval store and change manager around a
``SessionManager`` over scripted in-memory transports, so every test walks the
real validation / redaction / approval paths without touching the network.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.changes.backup import EncryptedBackupVault
from incidentlens_control_plane.changes.manager import ChangeManager
from incidentlens_control_plane.changes.store import ChangeSetStore
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.evidence.service import EvidenceService
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.evidence.types import EvidenceKind
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.tool_executor import ToolExecutor
from incidentlens_control_plane.investigation.tools import (
    TOOL_CONTAINER_READ,
    TOOL_DEFINITIONS,
    TOOL_DELEGATE_CHILD,
    TOOL_DOCKER_ACTION,
    TOOL_EVIDENCE_LIST,
    TOOL_EVIDENCE_READ,
    TOOL_FILE_EDIT,
    TOOL_HOST_LIST,
    TOOL_HOST_READ,
    TOOL_HOST_SEARCH,
    TOOL_HOST_STAT,
    TOOL_LOG_CONTEXT,
    TOOL_LOG_QUERY,
    TOOL_LOG_SEARCH,
    TOOL_REGISTRY_INFO,
    TOOL_SHELL_EXEC,
    TOOL_SOURCE_DISCOVER,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    EvidenceReference,
    Investigation,
    InvestigationBudget,
    UsageCounters,
)
from incidentlens_control_plane.logs.service import LogService
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.fakes import FakeChangeTransport
from incidentlens_control_plane.remote_ops.gateway import RemoteToolGateway
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.transport import (
    CommandResult,
    RemoteConnectionError,
)

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)

PROJECT_ID = "payments"
TARGET_ID = "dev-a"
SERVICE = "payment-api"
CONTAINER = "payments-api-1"
HOST_ROOT = PurePosixPath("/opt/payments")
LOG_PATH = PurePosixPath("/var/log/payment/app.log")
CONTAINER_ROOT = PurePosixPath("/app")
LOG_ROOT = LOG_PATH.parent


def _project_registration() -> ProjectRegistration:
    return ProjectRegistration(
        project_id=PROJECT_ID,
        display_name="Payments",
        targets=(
            TargetRegistration(
                target_id=TARGET_ID,
                host="dev-a.example.test",
                ssh_user="deploy",
                ssh_config_alias="dev-a",
            ),
        ),
        services=(
            ServiceRegistration(
                compose_service=SERVICE,
                container_names=(CONTAINER, "payments-api-2"),
                allowed_log_paths=(str(LOG_PATH),),
                allowed_host_paths=(HOST_ROOT,),
                allowed_container_paths=(CONTAINER_ROOT,),
                container_path_hints=("/app/logs",),
                protected_remote_paths=(HOST_ROOT / "app.env",),
            ),
        ),
    )


def make_scope(
    *,
    scope_kind: LogScope = LogScope.HOST,
    allowed_host_paths: tuple[PurePosixPath, ...] = (HOST_ROOT, LOG_ROOT),
    allowed_container_paths: tuple[PurePosixPath, ...] = (CONTAINER_ROOT,),
) -> AgentScope:
    if scope_kind is LogScope.CONTAINER:
        return AgentScope(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            scope=LogScope.CONTAINER,
            service_name=SERVICE,
            container_name=CONTAINER,
            allowed_host_paths=allowed_host_paths,
            allowed_container_paths=allowed_container_paths,
        )
    return AgentScope(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        scope=LogScope.HOST,
        allowed_host_paths=allowed_host_paths,
        allowed_container_paths=allowed_container_paths,
    )


def _new_run(
    investigations: InvestigationStore,
    *,
    run_id: str = "run-1",
    scope: AgentScope | None = None,
    budget: AgentBudget | None = None,
    evidence: tuple[EvidenceReference, ...] = (),
) -> AgentRun:
    scope = scope or make_scope()
    investigations.create_investigation(
        Investigation(
            investigation_id="inv-1",
            incident_id="inc-1",
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            symptom="checkout requests are failing",
            status=InvestigationStatus.RUNNING,
            budget=InvestigationBudget(),
            usage=UsageCounters(),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    run = AgentRun(
        agent_run_id=run_id,
        investigation_id="inv-1",
        parent_run_id=None,
        kind=AgentRunKind.PARENT,
        scope=scope,
        status=AgentRunStatus.RUNNING,
        budget=budget or AgentBudget(),
        usage=UsageCounters(),
        evidence=evidence,
        created_at=NOW,
        updated_at=NOW,
    )
    investigations.create_agent_run(run)
    return run


# ---------------------------------------------------------------------------
# Scripted in-memory remote processes and transports
# ---------------------------------------------------------------------------


class ScriptedShellProcess:
    """A ``RemoteProcess`` that resolves the framing marker and echoes a result."""

    def __init__(self, output: bytes = b"", status: int = 0) -> None:
        self.written = b""
        self.output = output
        self.status = status
        self.closed = False
        self._emitted = False

    async def write(self, data: bytes) -> None:
        self.written += data

    async def read(self, max_bytes: int) -> bytes:  # noqa: ARG002
        if self._emitted:
            return b""
        self._emitted = True
        marker = self._find_marker()
        if marker is None:
            return b""
        return self.output + b"\n" + marker + b":" + str(self.status).encode() + b"\n"

    def _find_marker(self) -> bytes | None:
        text = self.written.decode("utf-8", errors="replace")
        prefix = "__INCIDENTLENS_END_"
        start = text.find(prefix)
        if start == -1:
            return None
        end = text.find("__", start + len(prefix))
        if end == -1:
            return None
        return f"__INCIDENTLENS_END_{text[start + len(prefix):end]}__".encode()

    async def read_stderr(self, max_bytes: int) -> bytes:  # noqa: ARG002
        return b""

    async def close(self) -> None:
        self.closed = True


class HangingProcess:
    """A ``RemoteProcess`` whose reads never return, to trigger a shell timeout."""

    async def write(self, data: bytes) -> None:  # noqa: ARG002
        pass

    async def read(self, max_bytes: int) -> bytes:  # noqa: ARG002
        await asyncio.sleep(60)
        return b""

    async def read_stderr(self, max_bytes: int) -> bytes:  # noqa: ARG002
        await asyncio.sleep(60)
        return b""

    async def close(self) -> None:
        pass


class HarnessTransport:
    """A scripted transport: host files plus configurable shell/docker behavior."""

    def __init__(
        self,
        target: TargetRegistration,
        *,
        shell_output: bytes = b"",
        shell_status: int = 0,
        hang_shell: bool = False,
    ) -> None:
        self._target = target
        self.files: dict[PurePosixPath, bytes] = {}
        self.docker_logs: dict[tuple[str, int], bytes] = {}
        self.container_files: dict[PurePosixPath, bytes] = {}
        self.shells: list[ScriptedShellProcess | HangingProcess] = []
        self.run_argv_calls: list[tuple[str, ...]] = []
        self._shell_output = shell_output
        self._shell_status = shell_status
        self._hang_shell = hang_shell
        self.closed = False

    async def is_alive(self) -> bool:
        return not self.closed

    async def realpath(self, path: PurePosixPath) -> PurePosixPath:
        return path

    async def lstat(self, path: PurePosixPath) -> Any:
        data = self.files.get(path, b"")
        return _metadata(path, len(data))

    async def read_bytes(
        self, path: PurePosixPath, *, offset: int = 0, max_bytes: int
    ) -> bytes:
        return self.files.get(path, b"")[offset : offset + max_bytes]

    async def list_directory(self, path: PurePosixPath) -> tuple[Any, ...]:
        return tuple(
            _metadata(file_path, len(data))
            for file_path, data in self.files.items()
            if file_path.parent == path
        )

    async def write_bytes(
        self, path: PurePosixPath, content: bytes, *, mode: int = 0o644, exclusive: bool = False
    ) -> None:
        self.files[path] = content

    async def rename(self, source: PurePosixPath, target: PurePosixPath) -> None:
        if source in self.files:
            self.files[target] = self.files.pop(source)

    async def remove_file(self, path: PurePosixPath) -> None:
        self.files.pop(path, None)

    async def copy_file(
        self, source: PurePosixPath, target: PurePosixPath, *, preserve: bool = True
    ) -> None:
        if source in self.files:
            self.files[target] = self.files[source]

    async def run_argv(
        self, argv: tuple[str, ...], *, timeout: float = 30.0
    ) -> CommandResult:
        self.run_argv_calls.append(argv)
        simulated = self._simulate_docker(argv)
        if simulated is not None:
            return simulated
        return CommandResult(exit_status=0, stdout=b"", stderr=b"")

    def _simulate_docker(self, argv: tuple[str, ...]) -> CommandResult | None:
        if (
            len(argv) >= 7
            and argv[:5] == ("docker", "logs", "--timestamps", "--tail")
            and argv[5] == "--"
        ):
            container = argv[6]
            lines = self.docker_logs.get((container, int(argv[4])), b"").splitlines()
            return CommandResult(
                exit_status=0,
                stdout=("\n".join(lines) + ("\n" if lines else "")).encode(),
                stderr=b"",
            )
        if (
            len(argv) >= 5
            and argv[:2] == ("docker", "exec")
            and argv[3] == "cat"
            and "--" in argv
        ):
            path = PurePosixPath(argv[argv.index("--") + 1])
            return CommandResult(
                exit_status=0, stdout=self.container_files.get(path, b""), stderr=b""
            )
        if len(argv) >= 5 and argv[:2] == ("docker", "exec") and argv[3] == "stat" and "--" in argv:
            path = PurePosixPath(argv[argv.index("--") + 1])
            data = self.container_files.get(path, b"")
            return CommandResult(
                exit_status=0,
                stdout=f"regular file|{len(data)}|644|1000|1000|0".encode(),
                stderr=b"",
            )
        if len(argv) >= 8 and argv[:2] == ("docker", "exec") and argv[3] == "find":
            root = PurePosixPath(argv[4])
            lines = [
                f"{path}|f|{len(data)}|644|1000|1000|0"
                for path, data in self.container_files.items()
                if path.parent == root
            ]
            return CommandResult(
                exit_status=0,
                stdout=("\n".join(lines) + ("\n" if lines else "")).encode(),
                stderr=b"",
            )
        return None

    async def open_shell(self) -> ScriptedShellProcess | HangingProcess:
        if self._hang_shell:
            process: ScriptedShellProcess | HangingProcess = HangingProcess()
        else:
            process = ScriptedShellProcess(self._shell_output, self._shell_status)
        self.shells.append(process)
        return process

    async def open_process(self, argv: tuple[str, ...], *, term_type: str | None) -> Any:
        return ScriptedShellProcess()

    async def close(self) -> None:
        self.closed = True


def _metadata(path: PurePosixPath, size: int) -> Any:
    from incidentlens_control_plane.remote_ops.transport import FileMetadata

    return FileMetadata(
        path=path,
        size=size,
        mode=0o644,
        uid=1000,
        gid=1000,
        modified_ns=0,
        is_symlink=False,
    )


class HarnessTransportFactory:
    """Returns one live ``HarnessTransport`` per target, mirroring SessionManager."""

    def __init__(
        self,
        *,
        shell_output: bytes = b"",
        shell_status: int = 0,
        hang_shell: bool = False,
    ) -> None:
        self._live: dict[str, HarnessTransport] = {}
        self.transports: list[HarnessTransport] = []
        self._shell_output = shell_output
        self._shell_status = shell_status
        self._hang_shell = hang_shell

    async def connect(self, target: TargetRegistration) -> HarnessTransport:
        existing = self._live.get(target.target_id)
        if existing is not None:
            return existing
        transport = HarnessTransport(
            target,
            shell_output=self._shell_output,
            shell_status=self._shell_status,
            hang_shell=self._hang_shell,
        )
        self._live[target.target_id] = transport
        self.transports.append(transport)
        return transport


class OneTransportFactory:
    """Always returns one fixed transport (for docker/container scenarios)."""

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    async def connect(self, target: TargetRegistration) -> Any:  # noqa: ARG002
        return self._transport


class FailingTransportFactory:
    """A factory whose connect raises, to exercise the UNCERTAIN path."""

    async def connect(self, target: TargetRegistration) -> Any:  # noqa: ARG002
        raise RemoteConnectionError("connection refused")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    projects: ProjectRegistryStore
    sessions: SessionManager
    gateway: RemoteToolGateway
    log_store: LogStore
    logs: LogService
    evidence: EvidenceService
    evidence_store: EvidenceStore
    investigations: InvestigationStore
    approvals: ApprovalService
    executor: ToolExecutor
    factory: HarnessTransportFactory | OneTransportFactory | FailingTransportFactory


def build_harness(
    tmp_path: Path,
    *,
    transport_factory: Any = None,
) -> Harness:
    db_path = tmp_path / "runtime.db"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    projects = ProjectRegistryStore(connect)
    events = RuntimeEventStore(connect)
    approval_store = ApprovalStore(connect)
    change_store = ChangeSetStore(connect)
    log_store = LogStore(connect)
    evidence_store = EvidenceStore(connect)
    investigations = InvestigationStore(connect)
    for store in (
        projects,
        events,
        approval_store,
        change_store,
        log_store,
        evidence_store,
        investigations,
    ):
        store.migrate()

    projects.create(_project_registration(), now=NOW)

    broker = RuntimeEventBroker()
    approvals = ApprovalService(approvals=approval_store, events=events, broker=broker)
    factory = transport_factory or HarnessTransportFactory()
    sessions = SessionManager(factory)
    backups = EncryptedBackupVault(tmp_path / "vault", tmp_path / "vault.key")
    changes = ChangeManager(
        store=change_store,
        vault=backups,
        approvals=approvals,
        events=events,
        broker=broker,
        projects=projects,
        sessions=sessions,
    )
    gateway = RemoteToolGateway(
        projects=projects,
        sessions=sessions,
        changes=changes,
        approvals=approvals,
        events=events,
        broker=broker,
    )
    logs = LogService(
        projects=projects,
        store=log_store,
        sessions=sessions,
        evidence=evidence_store,
    )
    evidence = EvidenceService(evidence_store, investigations=investigations)
    executor = ToolExecutor(
        projects=projects,
        sessions=sessions,
        gateway=gateway,
        logs=logs,
        log_store=log_store,
        evidence=evidence,
        evidence_store=evidence_store,
        investigations=investigations,
        approvals=approvals,
    )
    return Harness(
        projects=projects,
        sessions=sessions,
        gateway=gateway,
        log_store=log_store,
        logs=logs,
        evidence=evidence,
        evidence_store=evidence_store,
        investigations=investigations,
        approvals=approvals,
        executor=executor,
        factory=factory,
    )


def tool_request(tool_name: str, tool_call_id: str = "call-1", **arguments: Any) -> Any:
    from incidentlens_control_plane.investigation.provider import ToolRequest

    return ToolRequest(tool_call_id=tool_call_id, tool_name=tool_name, arguments=arguments)


async def seed_host_log_file(harness: Harness, text: str) -> None:
    await seed_host_file(harness, LOG_PATH, text.encode())


async def seed_host_file(harness: Harness, path: PurePosixPath, content: bytes) -> None:
    """Connect once so the live transport exists, then seed a host file."""
    target = harness.projects.get(PROJECT_ID).targets[0]
    session = await harness.sessions.connect(target)
    session.transport.files[path] = content


# ---------------------------------------------------------------------------
# Registry / schema surface
# ---------------------------------------------------------------------------


def test_registry_materializes_every_definition_as_a_provider_schema(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    schemas = harness.executor.tool_schemas(scope=LogScope.HOST)
    by_name = {schema.tool_name: schema for schema in schemas}
    assert len(by_name) == len(TOOL_DEFINITIONS)
    for definition in TOOL_DEFINITIONS:
        schema = by_name[definition.tool_name]
        assert schema.parameters_json_schema == definition.parameters_json_schema
        assert schema.allowed_scope is definition.allowed_scope
        assert schema.requires_approval is definition.requires_approval


def test_tool_schemas_filter_by_run_scope(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    host_names = {s.tool_name for s in harness.executor.tool_schemas(scope=LogScope.HOST)}
    container_names = {
        s.tool_name for s in harness.executor.tool_schemas(scope=LogScope.CONTAINER)
    }
    # shell_exec is host-scope only and must disappear for a container run.
    assert TOOL_SHELL_EXEC in host_names
    assert TOOL_SHELL_EXEC not in container_names


def test_docker_action_is_statically_approval_gated(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    assert harness.executor.requires_approval(TOOL_DOCKER_ACTION) is True
    assert harness.executor.requires_approval(TOOL_HOST_READ) is False


# ---------------------------------------------------------------------------
# Executor validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_rejects_unregistered_tool(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request("no_such_tool"), run, now=NOW
    )
    assert outcome.status is ToolCallStatus.FAILED
    assert "not registered" in outcome.error_redacted


@pytest.mark.asyncio
async def test_execute_rejects_invalid_arguments(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(TOOL_HOST_READ, path="relative/path"), run, now=NOW
    )
    assert outcome.status is ToolCallStatus.FAILED
    assert "arguments invalid" in outcome.error_redacted


@pytest.mark.asyncio
async def test_execute_rejects_scope_mismatch(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations, scope=make_scope(scope_kind=LogScope.CONTAINER))
    outcome = await harness.executor.execute(
        tool_request(TOOL_SHELL_EXEC, service_name=SERVICE, command="pwd"), run, now=NOW
    )
    assert outcome.status is ToolCallStatus.FAILED
    assert "requires host scope" in outcome.error_redacted


@pytest.mark.asyncio
async def test_unregistered_service_rejected(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(TOOL_HOST_READ, service_name="ghost", path="/opt/payments/x.txt"),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.FAILED
    assert "ghost" in outcome.error_redacted


@pytest.mark.asyncio
async def test_unregistered_container_rejected(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_CONTAINER_READ,
            service_name=SERVICE,
            container="ghost-container",
            path="/app/config.yaml",
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.FAILED
    assert "not registered" in outcome.error_redacted


@pytest.mark.asyncio
async def test_path_outside_run_allowed_host_paths_rejected(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(TOOL_HOST_READ, service_name=SERVICE, path="/etc/passwd"),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.FAILED
    assert "outside the run's allowed host paths" in outcome.error_redacted


@pytest.mark.asyncio
async def test_container_run_may_only_read_its_own_container(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations, scope=make_scope(scope_kind=LogScope.CONTAINER))
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_CONTAINER_READ,
            service_name=SERVICE,
            container="payments-api-2",
            path="/app/config.yaml",
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.FAILED
    assert "only operate on its own container" in outcome.error_redacted


@pytest.mark.asyncio
async def test_container_run_may_only_query_its_own_service_logs(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations, scope=make_scope(scope_kind=LogScope.CONTAINER))
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_LOG_QUERY,
            service_name=SERVICE,
            source_kind="docker",
            source_ref="payments-api-2",
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.FAILED
    assert "only operate on its own container" in outcome.error_redacted


# ---------------------------------------------------------------------------
# Log tools: evidence-first, redaction, bounded summaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_query_persists_and_creates_run_scoped_log_evidence(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await seed_host_log_file(harness, "ERROR timeout token=abc123 request_id=req-1\n")
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_LOG_QUERY,
            service_name=SERVICE,
            source_kind="file",
            source_ref=str(LOG_PATH),
            tail_lines=10,
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert len(outcome.evidence) == 1
    ref = outcome.evidence[0]
    stored = harness.evidence_store.get(ref.evidence_id)
    assert stored.evidence_kind is EvidenceKind.LOG_RECORD
    assert stored.agent_run_id == run.agent_run_id
    # Evidence is stored with the redacted message, never the raw token.
    assert "abc123" not in stored.content_redacted
    assert "token=[REDACTED_TOKEN]" in stored.content_redacted
    # The model-visible summary is bounded and never leaks the raw token.
    assert len(outcome.summary) <= 4_000
    assert "abc123" not in outcome.summary
    assert ref.evidence_id.startswith("ev-")


@pytest.mark.asyncio
async def test_log_query_matches_severity_filter(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await seed_host_log_file(
        harness, "ERROR boom\nINFO fine\nWARN meh\n"
    )
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_LOG_QUERY,
            service_name=SERVICE,
            source_kind="file",
            source_ref=str(LOG_PATH),
            severity="error",
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert len(outcome.evidence) == 1
    assert "boom" in outcome.summary
    assert "fine" not in outcome.summary


@pytest.mark.asyncio
async def test_log_search_filters_by_correlation_key(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await seed_host_log_file(
        harness,
        "ERROR timeout request_id=req-9\nINFO ok request_id=req-9\nINFO other request_id=other-1\n",
    )
    run = _new_run(harness.investigations)
    await harness.executor.execute(
        tool_request(
            TOOL_LOG_QUERY,
            service_name=SERVICE,
            source_kind="file",
            source_ref=str(LOG_PATH),
        ),
        run,
        now=NOW,
    )
    outcome = await harness.executor.execute(
        tool_request(TOOL_LOG_SEARCH, correlation_key="request:req-9"), run, now=NOW
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert len(outcome.evidence) == 2
    assert "req-9" in outcome.summary
    assert "other-1" not in outcome.summary


@pytest.mark.asyncio
async def test_log_search_filters_by_normal_signal(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await seed_host_log_file(
        harness,
        "INFO GET /health 200\nINFO checkout failed 500\n",
    )
    run = _new_run(harness.investigations)
    await harness.executor.execute(
        tool_request(
            TOOL_LOG_QUERY,
            service_name=SERVICE,
            source_kind="file",
            source_ref=str(LOG_PATH),
        ),
        run,
        now=NOW,
    )
    outcome = await harness.executor.execute(
        tool_request(TOOL_LOG_SEARCH, normal_signal="healthcheck_ok"), run, now=NOW
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert len(outcome.evidence) == 1
    assert "checkout failed" not in outcome.summary


@pytest.mark.asyncio
async def test_log_context_returns_correlation_chain(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await seed_host_log_file(
        harness,
        "INFO start request_id=req-7\nERROR timeout request_id=req-7\nINFO done request_id=req-7\n",
    )
    run = _new_run(harness.investigations)
    await harness.executor.execute(
        tool_request(
            TOOL_LOG_QUERY,
            service_name=SERVICE,
            source_kind="file",
            source_ref=str(LOG_PATH),
        ),
        run,
        now=NOW,
    )
    outcome = await harness.executor.execute(
        tool_request(TOOL_LOG_CONTEXT, correlation_key="request:req-7"), run, now=NOW
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert len(outcome.evidence) == 3
    assert "request:req-7" in outcome.summary


@pytest.mark.asyncio
async def test_log_query_respects_per_tool_output_budget(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await seed_host_log_file(
        harness, "".join(f"INFO line-{i} request_id=r{i}\n" for i in range(20))
    )
    budget = AgentBudget(max_output_bytes_per_tool=120)
    run = _new_run(harness.investigations, budget=budget)
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_LOG_QUERY,
            service_name=SERVICE,
            source_kind="file",
            source_ref=str(LOG_PATH),
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert outcome.output_bytes <= 120
    assert len(outcome.evidence) < 20
    assert "dropped" in outcome.summary


@pytest.mark.asyncio
async def test_docker_log_query_validates_registered_container(tmp_path: Path) -> None:
    transport = FakeChangeTransport()
    transport.docker_logs[(CONTAINER, 10)] = (
        b"2026-08-12T10:00:00Z ERROR secret=abc123\n"
    )
    harness = build_harness(tmp_path, transport_factory=OneTransportFactory(transport))
    run = _new_run(harness.investigations, scope=make_scope(scope_kind=LogScope.CONTAINER))
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_LOG_QUERY,
            service_name=SERVICE,
            source_kind="docker",
            source_ref=CONTAINER,
            tail_lines=10,
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert len(outcome.evidence) == 1
    assert "abc123" not in outcome.summary
    assert transport.run_argv_calls[0][:4] == ("docker", "logs", "--timestamps", "--tail")


# ---------------------------------------------------------------------------
# Evidence tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_read_enforces_run_ownership(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(TOOL_EVIDENCE_READ, evidence_id="ev-not-owned"), run, now=NOW
    )
    assert outcome.status is ToolCallStatus.FAILED
    assert "not collected by this run" in outcome.error_redacted


@pytest.mark.asyncio
async def test_evidence_read_returns_redacted_excerpt(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await seed_host_file(harness, HOST_ROOT / "app.conf", b"token=secret123\n")
    run = _new_run(harness.investigations)
    first = await harness.executor.execute(
        tool_request(TOOL_HOST_READ, service_name=SERVICE, path=str(HOST_ROOT / "app.conf")),
        run,
        now=NOW,
    )
    owned_run = run.model_copy(update={"evidence": first.evidence})
    outcome = await harness.executor.execute(
        tool_request(TOOL_EVIDENCE_READ, evidence_id=first.evidence[0].evidence_id),
        owned_run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert "secret123" not in outcome.summary
    assert first.evidence[0].evidence_id in outcome.summary


@pytest.mark.asyncio
async def test_evidence_list_returns_run_evidence(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await seed_host_file(harness, HOST_ROOT / "app.conf", b"alpha\n")
    run = _new_run(harness.investigations)
    first = await harness.executor.execute(
        tool_request(TOOL_HOST_READ, service_name=SERVICE, path=str(HOST_ROOT / "app.conf")),
        run,
        now=NOW,
    )
    outcome = await harness.executor.execute(
        tool_request(TOOL_EVIDENCE_LIST), run, now=NOW
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert first.evidence[0].evidence_id in outcome.summary
    assert len(outcome.evidence) == 1


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_read_creates_redacted_file_snapshot_evidence(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await seed_host_file(
        harness, HOST_ROOT / "app.conf", b"api_key=hunter2\nlistening on 10.1.2.3\n"
    )
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(TOOL_HOST_READ, service_name=SERVICE, path=str(HOST_ROOT / "app.conf")),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    ref = outcome.evidence[0]
    stored = harness.evidence_store.get(ref.evidence_id)
    assert stored.evidence_kind is EvidenceKind.FILE_SNAPSHOT
    assert "hunter2" not in stored.content_redacted
    assert "10.1.2.3" not in stored.content_redacted
    assert "hunter2" not in outcome.summary


@pytest.mark.asyncio
async def test_container_read_validates_and_reads(tmp_path: Path) -> None:
    transport = FakeChangeTransport()
    transport.container_files[PurePosixPath("/app/config.yaml")] = b"token=abc123\n"
    harness = build_harness(tmp_path, transport_factory=OneTransportFactory(transport))
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_CONTAINER_READ,
            service_name=SERVICE,
            container=CONTAINER,
            path="/app/config.yaml",
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    stored = harness.evidence_store.get(outcome.evidence[0].evidence_id)
    assert "abc123" not in stored.content_redacted
    assert "abc123" not in outcome.summary


@pytest.mark.asyncio
async def test_host_search_creates_evidence(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await seed_host_file(harness, HOST_ROOT / "a.py", b"needle here\nother\n")
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_HOST_SEARCH,
            service_name=SERVICE,
            path=str(HOST_ROOT),
            query="needle",
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert len(outcome.evidence) == 1
    assert "needle" in outcome.summary


@pytest.mark.asyncio
async def test_host_list_and_stat_produce_evidence(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await seed_host_file(harness, HOST_ROOT / "a.py", b"x" * 10)
    run = _new_run(harness.investigations)
    listing = await harness.executor.execute(
        tool_request(TOOL_HOST_LIST, service_name=SERVICE, path=str(HOST_ROOT)),
        run,
        now=NOW,
    )
    assert listing.status is ToolCallStatus.SUCCEEDED
    assert "a.py" in listing.summary
    stat = await harness.executor.execute(
        tool_request(TOOL_HOST_STAT, service_name=SERVICE, path=str(HOST_ROOT / "a.py")),
        run,
        now=NOW,
    )
    assert stat.status is ToolCallStatus.SUCCEEDED
    assert "size=10" in stat.summary


# ---------------------------------------------------------------------------
# Registry info / source discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_info_records_discovery_evidence(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(TOOL_REGISTRY_INFO), run, now=NOW
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert len(outcome.evidence) == 1
    stored = harness.evidence_store.get(outcome.evidence[0].evidence_id)
    assert stored.evidence_kind is EvidenceKind.REGISTRY_DISCOVERY
    assert SERVICE in outcome.summary
    assert CONTAINER in outcome.summary


@pytest.mark.asyncio
async def test_source_discover_lists_containers_and_log_paths(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(TOOL_SOURCE_DISCOVER, service_name=SERVICE), run, now=NOW
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert len(outcome.evidence) == 1
    stored = harness.evidence_store.get(outcome.evidence[0].evidence_id)
    assert stored.evidence_kind is EvidenceKind.REGISTRY_DISCOVERY
    assert f"container:{CONTAINER}" in outcome.summary
    assert f"log-path:{LOG_PATH}" in outcome.summary


# ---------------------------------------------------------------------------
# Approval-gated shell / changeset / docker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shell_read_only_command_executes_and_records_evidence(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, transport_factory=HarnessTransportFactory(
        shell_output=b"payments-api-1 running", shell_status=0
    ))
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(TOOL_SHELL_EXEC, service_name=SERVICE, command="docker ps"),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    ref = outcome.evidence[0]
    stored = harness.evidence_store.get(ref.evidence_id)
    assert stored.evidence_kind is EvidenceKind.COMMAND_OUTPUT
    assert "payments-api-1 running" in stored.content_redacted
    assert ref.evidence_id.startswith("ev-")


@pytest.mark.asyncio
async def test_shell_mutating_command_requests_approval(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(TOOL_SHELL_EXEC, service_name=SERVICE, command="systemctl restart mysql"),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.WAITING_APPROVAL
    assert outcome.approval_id is not None
    # No transport contact happened while awaiting approval.
    assert harness.factory.transports == []
    pending = harness.approvals.list()
    assert len(pending) == 1
    assert pending[0].intent["kind"] == "shell"


@pytest.mark.asyncio
async def test_shell_forbidden_command_fails_without_transport(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(TOOL_SHELL_EXEC, service_name=SERVICE, command="rm -rf /opt/payments"),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.FAILED
    assert "forbidden" in outcome.error_redacted
    assert harness.factory.transports == []
    assert harness.approvals.list() == ()


@pytest.mark.asyncio
async def test_shell_approved_command_executes_after_approval(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        transport_factory=HarnessTransportFactory(
            shell_output=b"restarted", shell_status=0
        ),
    )
    run = _new_run(harness.investigations)
    first = await harness.executor.execute(
        tool_request(TOOL_SHELL_EXEC, service_name=SERVICE, command="systemctl restart mysql"),
        run,
        now=NOW,
    )
    assert first.status is ToolCallStatus.WAITING_APPROVAL
    await harness.approvals.approve(first.approval_id)
    second = await harness.executor.execute(
        tool_request(TOOL_SHELL_EXEC, service_name=SERVICE, command="systemctl restart mysql"),
        run,
        now=NOW,
        approval_id=first.approval_id,
    )
    assert second.status is ToolCallStatus.SUCCEEDED
    stored = harness.evidence_store.get(second.evidence[0].evidence_id)
    assert stored.evidence_kind is EvidenceKind.COMMAND_OUTPUT
    assert "restarted" in stored.content_redacted


@pytest.mark.asyncio
async def test_docker_action_requests_approval_first(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_DOCKER_ACTION,
            service_name=SERVICE,
            action="restart",
            container=CONTAINER,
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.WAITING_APPROVAL
    assert outcome.approval_id is not None
    pending = harness.approvals.list()
    assert len(pending) == 1
    assert pending[0].intent["kind"] == "docker_action"
    assert pending[0].intent["container"] == CONTAINER


@pytest.mark.asyncio
async def test_docker_action_approved_executes_and_records_evidence(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    first = await harness.executor.execute(
        tool_request(
            TOOL_DOCKER_ACTION,
            service_name=SERVICE,
            action="restart",
            container=CONTAINER,
        ),
        run,
        now=NOW,
    )
    await harness.approvals.approve(first.approval_id)
    second = await harness.executor.execute(
        tool_request(
            TOOL_DOCKER_ACTION,
            service_name=SERVICE,
            action="restart",
            container=CONTAINER,
        ),
        run,
        now=NOW,
        approval_id=first.approval_id,
    )
    assert second.status is ToolCallStatus.SUCCEEDED
    stored = harness.evidence_store.get(second.evidence[0].evidence_id)
    assert stored.evidence_kind is EvidenceKind.VALIDATION_RESULT
    assert "restart" in second.summary


@pytest.mark.asyncio
async def test_file_edit_protected_path_requests_exact_approval(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await seed_host_file(harness, HOST_ROOT / "app.env", b"VERSION=1\n")
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_FILE_EDIT,
            service_name=SERVICE,
            path=str(HOST_ROOT / "app.env"),
            expected_sha256=_sha256(b"VERSION=1\n"),
            replacements=[{"old_text": "VERSION=1", "new_text": "VERSION=2"}],
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.WAITING_APPROVAL
    assert outcome.approval_id is not None
    pending = harness.approvals.list()
    assert len(pending) == 1
    assert pending[0].intent["kind"] == "change"
    assert pending[0].intent["paths_sha256"] is not None


@pytest.mark.asyncio
async def test_file_edit_unprotected_path_applies_without_approval(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await seed_host_file(harness, HOST_ROOT / "app.conf", b"value = 1\n")
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_FILE_EDIT,
            service_name=SERVICE,
            path=str(HOST_ROOT / "app.conf"),
            expected_sha256=_sha256(b"value = 1\n"),
            replacements=[{"old_text": "1", "new_text": "2"}],
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    assert "applied" in outcome.summary
    stored = harness.evidence_store.get(outcome.evidence[0].evidence_id)
    assert stored.evidence_kind is EvidenceKind.VALIDATION_RESULT
    # The remote file really changed.
    assert harness.factory.transports[0].files[HOST_ROOT / "app.conf"] == b"value = 2\n"


@pytest.mark.asyncio
async def test_file_edit_approved_protected_path_consumes_approval(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await seed_host_file(harness, HOST_ROOT / "app.env", b"VERSION=1\n")
    run = _new_run(harness.investigations)
    first = await harness.executor.execute(
        tool_request(
            TOOL_FILE_EDIT,
            service_name=SERVICE,
            path=str(HOST_ROOT / "app.env"),
            expected_sha256=_sha256(b"VERSION=1\n"),
            replacements=[{"old_text": "VERSION=1", "new_text": "VERSION=2"}],
        ),
        run,
        now=NOW,
    )
    assert first.status is ToolCallStatus.WAITING_APPROVAL
    await harness.approvals.approve(first.approval_id)
    second = await harness.executor.execute(
        tool_request(
            TOOL_FILE_EDIT,
            service_name=SERVICE,
            path=str(HOST_ROOT / "app.env"),
            expected_sha256=_sha256(b"VERSION=1\n"),
            replacements=[{"old_text": "VERSION=1", "new_text": "VERSION=2"}],
        ),
        run,
        now=NOW,
        approval_id=first.approval_id,
    )
    assert second.status is ToolCallStatus.SUCCEEDED
    assert harness.factory.transports[0].files[HOST_ROOT / "app.env"] == b"VERSION=2\n"


# ---------------------------------------------------------------------------
# Child delegation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_child_validates_scope_and_persists(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_DELEGATE_CHILD,
            child_run_id="child-1",
            task_prompt="inspect the payments-api container logs",
            scope={
                "project_id": PROJECT_ID,
                "target_id": TARGET_ID,
                "scope": "container",
                "service_name": SERVICE,
                "container_name": CONTAINER,
                "allowed_container_paths": ["/app"],
            },
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.SUCCEEDED
    package = harness.investigations.get_delegated_task("child-1")
    assert package.parent_run_id == run.agent_run_id
    assert package.scope.scope is LogScope.CONTAINER
    stored = harness.evidence_store.get(outcome.evidence[0].evidence_id)
    assert stored.evidence_kind is EvidenceKind.REGISTRY_DISCOVERY


@pytest.mark.asyncio
async def test_delegate_child_rejects_out_of_scope_narrowing(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_DELEGATE_CHILD,
            child_run_id="child-x",
            task_prompt="drift away",
            scope={
                "project_id": "other-project",
                "target_id": TARGET_ID,
                "scope": "host",
            },
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.FAILED
    assert "child scope" in outcome.error_redacted


@pytest.mark.asyncio
async def test_delegate_child_rejects_unowned_evidence(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_DELEGATE_CHILD,
            child_run_id="child-x",
            task_prompt="seed",
            evidence_ids=["ev-fabricated"],
            scope={
                "project_id": PROJECT_ID,
                "target_id": TARGET_ID,
                "scope": "host",
            },
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.FAILED
    assert "not owned by this run" in outcome.error_redacted


# ---------------------------------------------------------------------------
# Uncertain remote state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shell_timeout_marks_uncertain_and_records_evidence(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        transport_factory=HarnessTransportFactory(hang_shell=True),
    )
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(
            TOOL_SHELL_EXEC,
            service_name=SERVICE,
            command="docker ps",
            timeout_seconds=1,
        ),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.UNCERTAIN
    assert len(outcome.evidence) == 1
    stored = harness.evidence_store.get(outcome.evidence[0].evidence_id)
    assert stored.evidence_kind is EvidenceKind.UNCERTAIN_STATE
    assert "could not be confirmed" in outcome.summary


@pytest.mark.asyncio
async def test_connection_error_marks_uncertain(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, transport_factory=FailingTransportFactory())
    run = _new_run(harness.investigations)
    outcome = await harness.executor.execute(
        tool_request(TOOL_HOST_READ, service_name=SERVICE, path=str(HOST_ROOT / "a.txt")),
        run,
        now=NOW,
    )
    assert outcome.status is ToolCallStatus.UNCERTAIN
    stored = harness.evidence_store.get(outcome.evidence[0].evidence_id)
    assert stored.evidence_kind is EvidenceKind.UNCERTAIN_STATE


def _sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()
