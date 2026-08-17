"""Opt-in live acceptance test for the Phase 4 bounded agent runtime.

This test is DISABLED by default.  It exercises the real ``AsyncSshTransport``
against an ephemeral OpenSSH container (``infra/test-ssh``) and drives the real
runtime (``build_runtime``: orchestrator, investigation service, tool executor,
evidence, approvals, recovery) with the scripted ``FakeProvider`` — no real
model provider is ever contacted.  It verifies the Phase 4 acceptance points
over a real SSH/SFTP/shell transport:

1. Parent reads a real host log via ``log_query``, folds the redacted LOG_RECORD
   evidence into its run, and completes with a grounded conclusion that cites
   only evidence the run actually owns.
2. Parent concurrently delegates two container-scoped children; every child runs
   its own bounded loop against its own scope/session, and its evidence-grounded
   report (COMPLETE when docker is available on the target, PARTIAL when the
   child's container tooling fails) is folded into the parent as CHILD_REPORT
   evidence without disturbing the host session.
3. Approval pause/resume: a shell command that policy classifies as
   approval-required parks the run WAITING_APPROVAL; approving it re-executes
   the exact single-use intent once (the remote file is actually created) and
   resumes the run.
4. Restart checkpoint: a fresh runtime over the same ``data_dir`` restores the
   parked run; the approval decision then resumes it from its latest checkpoint
   and round 2 is not replayed (checkpoints 1-4, rounds 1-2).
5. Uncertain no-replay: an in-flight dangerous shell call left by a simulated
   crash is marked UNCERTAIN by startup recovery, the run parks
   PAUSED_UNCERTAIN_STATE, and resuming never re-executes it.

Run it only when Docker is available and you explicitly opt in::

    INCIDENTLENS_RUN_LIVE_AGENT_TESTS=1 UV_CACHE_DIR=.uv-cache uv run \\
        pytest tests/integration/test_live_agent_runtime.py -q

The fixture skips with a clear reason when the opt-in variable is absent, when
the Docker CLI/daemon or ``ssh-keygen`` is unavailable, or when the SSH target
never becomes reachable.  The private key is generated fresh under ``tmp_path``
and is never committed; the compose project is torn down with
``docker compose down --volumes`` in a finalizer.

Container-child sub-checks additionally require a ``docker`` CLI inside the SSH
target.  The stock ``infra/test-ssh`` image installs only OpenSSH, so against
that image the children return PARTIAL reports (a real crash/over-budget path)
and the test still passes: the parent folds the partial reports and completes.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest
from incidentlens_control_plane.approvals.types import ApprovalStatus
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.evidence.types import EvidenceKind
from incidentlens_control_plane.investigation.fake_provider import (
    DelegateChildStep,
    FakeProviderRegistry,
    RequestToolsStep,
    StopStep,
)
from incidentlens_control_plane.investigation.provider import (
    AgentTurnResult,
    ChildDelegationRequest,
    Conclusion,
    ConversationRequest,
    ModelProvider,
    StopReason,
    StopSignal,
    ToolRequest,
)
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    Investigation,
    InvestigationBudget,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.asyncssh_adapter import (
    AsyncSshTransportFactory,
)
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.runtime import RuntimeServices, build_runtime

PROJECT_NAME = "incidentlens-live-agent"
PROJECT_ID = "liveagentproj"
TARGET_ID = "live-agent-ssh"
SERVICE = "test-ssh"
WORKSPACE = PurePosixPath("/workspace/service")
LOG_FILE = WORKSPACE / "live.log"
APPROVAL_MARKER = WORKSPACE / "approval-marker"

# A secret-looking token seeded into the live log file.  The redaction pipeline
# must ensure it never survives into a stored ``LogRecord``, an evidence ref, or
# a conclusion summary.
_LIVE_TOKEN = "super-secret-live"
_SEED_LINES = f"INFO payment start\nWARN token={_LIVE_TOKEN}\n"

# Bounded budgets so a live run can never spin or wait forever.
_RUN_BUDGET = AgentBudget(max_rounds=8, max_tool_calls=16)


# ---------------------------------------------------------------------------
# Fixture: disposable OpenSSH container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveTarget:
    """Everything a live test needs to connect to the disposable target."""

    target: TargetRegistration
    service: ServiceRegistration
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
    if os.environ.get("INCIDENTLENS_RUN_LIVE_AGENT_TESTS") != "1":
        pytest.skip(
            "INCIDENTLENS_RUN_LIVE_AGENT_TESTS=1 is not set; the live agent test is opt-in"
        )

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker executable not found on PATH; cannot run the live agent test")
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        pytest.skip("ssh-keygen executable not found on PATH; cannot run the live agent test")

    daemon = subprocess.run([docker, "info"], capture_output=True, text=True, timeout=30)
    if daemon.returncode != 0:
        pytest.skip("docker daemon is not running; cannot start the disposable SSH target")

    tmp_path = tmp_path_factory.mktemp("live-agent")
    compose_file = (
        Path(__file__).resolve().parents[2] / "infra" / "test-ssh" / "compose.yaml"
    )
    key_path = tmp_path / "id_ed25519"

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
        allowed_log_paths=(str(LOG_FILE),),
        allowed_host_paths=(WORKSPACE,),
        allowed_container_paths=(WORKSPACE,),
    )
    factory = AsyncSshTransportFactory(
        client_key_paths=(str(key_path),),
        known_hosts_path=str(known_hosts_path),
    )

    asyncio.run(_wait_for_ssh(factory, target))

    return LiveTarget(
        target=target,
        service=service,
        factory=factory,
        container_name=container_name,
    )


# ---------------------------------------------------------------------------
# Grounding wrapper + runtime harness
# ---------------------------------------------------------------------------


class _GroundedStopProvider(ModelProvider):
    """Wrap the scripted provider to ground a bare COMPLETED stop.

    A ``StopStep`` that declares COMPLETED without a conclusion is completed
    with a grounded conclusion citing the run's latest evidence (from the
    bounded conversation messages), so the live test does not need to predict
    hash-derived evidence ids.
    """

    def __init__(self, delegate: ModelProvider) -> None:
        self._delegate = delegate

    async def generate_turn(self, request: ConversationRequest) -> AgentTurnResult:
        result = await self._delegate.generate_turn(request)
        if (
            result.stop_signal is not None
            and result.stop_signal.stop_reason is StopReason.COMPLETED
            and not result.conclusions
        ):
            latest = _latest_owned_evidence_id(request)
            if latest is not None:
                result = result.model_copy(
                    update={
                        "conclusions": (
                            Conclusion(
                                summary="root cause identified from collected evidence",
                                facts=(latest,),
                                evidence_ids=(latest,),
                            ),
                        )
                    }
                )
        return result


def _latest_owned_evidence_id(request: ConversationRequest) -> str | None:
    """Return the most recent evidence id the run owns, from the bounded context.

    Scans the transcript for the newest tool-result evidence, falling back to
    the synthesized header's "Evidence collected (recent)" list when no tool
    result has been persisted yet.
    """
    header_ids: list[str] = []
    for message in request.messages:
        for block in message.blocks:
            if isinstance(block, ToolResultBlock) and block.evidence_ids:
                return block.evidence_ids[0]
            if isinstance(block, TextBlock):
                marker = "Evidence collected (recent):"
                if marker in block.text:
                    tail = block.text.split(marker, 1)[1]
                    for line in tail.splitlines()[1:]:
                        if not line.startswith("- "):
                            break
                        candidate = line[2:].split(":", 1)[0].strip()
                        if candidate:
                            header_ids.append(candidate)
    if header_ids:
        return header_ids[-1]
    return None


def _build_runtime(
    tmp_path: Path, live: LiveTarget, registry: FakeProviderRegistry
) -> RuntimeServices:
    """Build a real runtime over the disposable SSH target with the scripted provider.

    Reuses the same ``data_dir`` every call so a later "restart" runtime sees the
    same SQLite store (investigations, checkpoints, evidence, approvals).
    """
    runtime = build_runtime(
        RuntimeSettings(data_dir=tmp_path / "data"),
        transport_factory=live.factory,
        fake_provider_registry=registry,
    )
    orchestrator = runtime.investigations._orchestrator  # type: ignore[attr-defined]
    orchestrator._provider = _GroundedStopProvider(orchestrator._provider)  # type: ignore[attr-defined]
    return runtime


def _register_project(runtime: RuntimeServices, live: LiveTarget) -> None:
    runtime.projects.create(
        ProjectRegistration(
            project_id=PROJECT_ID,
            display_name="Live Agent Acceptance",
            targets=(live.target,),
            services=(live.service,),
        ),
        now=datetime.now(UTC),
    )


def _host_scope() -> AgentScope:
    return AgentScope(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        scope=LogScope.HOST,
        allowed_host_paths=(WORKSPACE,),
    )


def _container_scope(container_name: str) -> AgentScope:
    return AgentScope(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        scope=LogScope.CONTAINER,
        service_name=SERVICE,
        container_name=container_name,
        allowed_container_paths=(WORKSPACE,),
    )


def _tool(tool_name: str, tool_call_id: str, **arguments: object) -> ToolRequest:
    return ToolRequest(
        tool_call_id=tool_call_id, tool_name=tool_name, arguments=arguments
    )


def _delegate(child_run_id: str, task_prompt: str, container_name: str) -> ChildDelegationRequest:
    return ChildDelegationRequest(
        child_run_id=child_run_id,
        task_prompt=task_prompt,
        scope=_container_scope(container_name),
        evidence_ids=(),
    )


def _create_investigation(
    runtime: RuntimeServices, investigation_id: str, *, incident_id: str
) -> Investigation:
    return runtime.investigations.create_investigation(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        service=SERVICE,
        symptom="checkout requests are failing",
        incident_id=incident_id,
    )


def _create_parent_run(
    runtime: RuntimeServices, investigation: Investigation, run_id: str, scope: AgentScope
) -> AgentRun:
    now = datetime.now(UTC)
    run = AgentRun(
        agent_run_id=run_id,
        investigation_id=investigation.investigation_id,
        parent_run_id=None,
        kind=AgentRunKind.PARENT,
        scope=scope,
        status=AgentRunStatus.CREATED,
        budget=_RUN_BUDGET,
        usage=UsageCounters(),
        created_at=now,
        updated_at=now,
    )
    runtime.investigation_store.create_agent_run(run)
    return run


# ---------------------------------------------------------------------------
# Remote helpers over the real transport
# ---------------------------------------------------------------------------


async def _write_remote_log(live: LiveTarget, content: bytes) -> None:
    sessions = SessionManager(live.factory)
    try:
        session = await sessions.connect(live.target)
        await session.transport.write_bytes(LOG_FILE, content, mode=0o644)
    finally:
        await sessions.close_all()


async def _target_has_docker(live: LiveTarget) -> bool:
    sessions = SessionManager(live.factory)
    try:
        session = await sessions.connect(live.target)
        result = await session.transport.run_argv(
            ("sh", "-c", "command -v docker"), timeout=15.0
        )
        return result.exit_status == 0
    finally:
        await sessions.close_all()


async def _remote_file_exists(live: LiveTarget, path: PurePosixPath) -> bool:
    sessions = SessionManager(live.factory)
    try:
        session = await sessions.connect(live.target)
        try:
            await session.transport.read_bytes(path, max_bytes=4096)
        except Exception:  # noqa: BLE001 - a missing file is the expected false case
            return False
        return True
    finally:
        await sessions.close_all()


def _seed_in_flight_shell(
    runtime: RuntimeServices, investigation_id: str, run_id: str, tool_call_id: str
) -> None:
    """Seed a dangerous in-flight shell call, as if the process crashed mid-execution.

    Written directly so startup recovery in a fresh runtime must classify it:
    the RUNNING ``shell_exec`` becomes UNCERTAIN (never replayed) and the run is
    parked PAUSED_UNCERTAIN_STATE.
    """
    now = datetime.now(UTC)
    store = runtime.investigation_store
    store.create_investigation(
        Investigation(
            investigation_id=investigation_id,
            incident_id="inc-live-uncertain",
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            service=SERVICE,
            symptom="uncertain restart recovery",
            status=InvestigationStatus.RUNNING,
            budget=InvestigationBudget(),
            usage=UsageCounters(),
            created_at=now,
            updated_at=now,
        )
    )
    store.create_agent_run(
        AgentRun(
            agent_run_id=run_id,
            investigation_id=investigation_id,
            parent_run_id=None,
            kind=AgentRunKind.PARENT,
            scope=_host_scope(),
            status=AgentRunStatus.RUNNING,
            budget=_RUN_BUDGET,
            usage=UsageCounters(),
            created_at=now,
            updated_at=now,
        )
    )
    store.create_tool_call(
        ToolCall(
            tool_call_id=tool_call_id,
            agent_run_id=run_id,
            tool_name="shell_exec",
            status=ToolCallStatus.RUNNING,
            idempotency_key=tool_call_id,
            planned_at=now,
        )
    )


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


def test_live_agent_runtime_parent_children_approval_restart_uncertain(
    live_target: LiveTarget, tmp_path: Path
) -> None:
    registry = FakeProviderRegistry()
    runtime1 = _build_runtime(tmp_path, live_target, registry)
    _register_project(runtime1, live_target)
    has_docker = asyncio.run(_target_has_docker(live_target))
    runtime2: RuntimeServices | None = None
    runtime3: RuntimeServices | None = None

    async def scenario() -> None:
        nonlocal runtime2, runtime3
        try:
            await _write_remote_log(live_target, _SEED_LINES.encode("utf-8"))

            # ---- Scenario A: parent reads logs, delegates 2 container children,
            #      folds reports, concludes on owned evidence. -----------------
            parent_a_id = "live-parent-a"
            inv_a = _create_investigation(
                runtime1, "inv-live-a", incident_id="inc-live-a"
            )
            _create_parent_run(runtime1, inv_a, parent_a_id, _host_scope())
            registry.set_script(
                parent_a_id,
                [
                    RequestToolsStep(
                        tool_requests=(
                            _tool(
                                "log_query",
                                "a-log",
                                service_name=SERVICE,
                                source_kind="file",
                                source_ref=str(LOG_FILE),
                                tail_lines=100,
                            ),
                        )
                    ),
                    DelegateChildStep(
                        delegation=_delegate(
                            "live-child-1", "inspect container one", live_target.container_name
                        )
                    ),
                    DelegateChildStep(
                        delegation=_delegate(
                            "live-child-2", "inspect container two", live_target.container_name
                        )
                    ),
                    StopStep(
                        stop_signal=StopSignal(
                            stop_reason=StopReason.COMPLETED, summary="parent done"
                        )
                    ),
                ],
            )
            for child_id in ("live-child-1", "live-child-2"):
                registry.set_script(
                    child_id,
                    [
                        RequestToolsStep(
                            tool_requests=(
                                _tool(
                                    "container_read",
                                    f"{child_id}-read",
                                    service_name=SERVICE,
                                    container=live_target.container_name,
                                    path=str(LOG_FILE),
                                ),
                            )
                        ),
                        StopStep(
                            stop_signal=StopSignal(
                                stop_reason=StopReason.COMPLETED, summary=f"{child_id} done"
                            )
                        ),
                    ],
                )
            parent_a = await asyncio.wait_for(
                runtime1.investigations.start(inv_a.investigation_id, _host_scope()),
                timeout=120.0,
            )

            assert parent_a.status is AgentRunStatus.COMPLETED
            assert parent_a.stop_reason is StopReason.COMPLETED
            assert (
                runtime1.investigations.get_investigation(inv_a.investigation_id).status
                is InvestigationStatus.COMPLETED
            )
            # Grounded conclusion: every citation resolves to evidence the run owns.
            conclusions = runtime1.investigation_store.list_conclusions(
                agent_run_id=parent_a_id
            )
            assert len(conclusions) == 1
            owned = {ref.evidence_id for ref in parent_a.evidence}
            assert conclusions[0].evidence_ids
            assert set(conclusions[0].evidence_ids) <= owned
            for evidence_id in conclusions[0].evidence_ids:
                ref = runtime1.evidence.get(evidence_id)
                assert _LIVE_TOKEN not in ref.content_redacted

            # The parent actually read the host log: LOG_RECORD evidence exists
            # and is redacted-only.
            log_evidence = runtime1.evidence.query(
                agent_run_id=parent_a_id, evidence_kind=EvidenceKind.LOG_RECORD
            )
            assert log_evidence
            assert all(_LIVE_TOKEN not in ref.content_redacted for ref in log_evidence)
            assert any("[REDACTED_TOKEN]" in ref.content_redacted for ref in log_evidence)

            # Both children were delegated concurrently and their reports were
            # folded in as CHILD_REPORT evidence (COMPLETE with docker, PARTIAL
            # without — a real crash path the parent must survive).
            children = [
                run
                for run in runtime1.investigation_store.list_agent_runs(
                    investigation_id=inv_a.investigation_id
                )
                if run.parent_run_id == parent_a_id
            ]
            assert len(children) == 2
            for child in children:
                if has_docker:
                    assert child.status is AgentRunStatus.COMPLETED
                else:
                    assert child.status is not AgentRunStatus.COMPLETED
            child_refs = [
                ref for ref in parent_a.evidence if ref.operation_id.startswith("child:")
            ]
            assert len(child_refs) == 2
            for ref in child_refs:
                stored = runtime1.evidence.get(ref.evidence_id)
                assert stored.evidence_kind is EvidenceKind.CHILD_REPORT

            # ---- Scenario B: approval pause/resume across a runtime restart. ---
            parent_b_id = "live-approval-parent"
            inv_b = _create_investigation(
                runtime1, "inv-live-b", incident_id="inc-live-b"
            )
            _create_parent_run(runtime1, inv_b, parent_b_id, _host_scope())
            registry.set_script(
                parent_b_id,
                [
                    RequestToolsStep(
                        tool_requests=(
                            _tool(
                                "shell_exec",
                                "b-touch",
                                service_name=SERVICE,
                                command=f"touch {APPROVAL_MARKER}",
                            ),
                        )
                    ),
                    StopStep(
                        stop_signal=StopSignal(
                            stop_reason=StopReason.COMPLETED, summary="approval done"
                        )
                    ),
                ],
            )
            parked = await asyncio.wait_for(
                runtime1.investigations.start(inv_b.investigation_id, _host_scope()),
                timeout=60.0,
            )
            assert parked.status is AgentRunStatus.WAITING_APPROVAL
            assert parked.stop_reason is StopReason.PENDING_APPROVAL
            assert (
                runtime1.investigations.get_investigation(inv_b.investigation_id).status
                is InvestigationStatus.WAITING_APPROVAL
            )
            tool_calls = runtime1.investigation_store.list_tool_calls(
                agent_run_id=parent_b_id
            )
            assert len(tool_calls) == 1
            approval_tool = tool_calls[0]
            assert approval_tool.status is ToolCallStatus.WAITING_APPROVAL
            assert approval_tool.approval_id is not None
            checkpoint_seqs = [
                cp.sequence
                for cp in runtime1.investigation_store.list_checkpoints(parent_b_id)
            ]
            assert checkpoint_seqs == [1, 2]
            round_numbers = [
                r.round_number
                for r in runtime1.investigation_store.list_rounds(parent_b_id)
            ]
            assert round_numbers == [1]

            # "Restart": a fresh runtime over the same data_dir restores the parked
            # run and its pending approval; recovery must not sweep either.
            runtime2 = _build_runtime(tmp_path, live_target, registry)
            await runtime2.recovery.startup()
            restored = runtime2.investigations.get_run(parent_b_id)
            assert restored.status is AgentRunStatus.WAITING_APPROVAL
            restored_tool = runtime2.investigation_store.get_tool_call(
                approval_tool.tool_call_id
            )
            assert restored_tool.status is ToolCallStatus.WAITING_APPROVAL
            pending = runtime2.approvals.get(approval_tool.approval_id)
            assert pending is not None and pending.status is ApprovalStatus.PENDING

            # Approve exactly once and let the decision re-execute + resume.
            await runtime2.approvals.approve(approval_tool.approval_id)
            outcome = await asyncio.wait_for(
                runtime2.investigations.handle_approval_decision(
                    approval_tool.approval_id
                ),
                timeout=60.0,
            )
            assert outcome.matched == "tool_call"
            assert outcome.action == "re-executed"
            assert outcome.applied is True

            final_b = runtime2.investigations.get_run(parent_b_id)
            assert final_b.status is AgentRunStatus.COMPLETED
            assert (
                runtime2.investigations.get_investigation(inv_b.investigation_id).status
                is InvestigationStatus.COMPLETED
            )
            # The single-use approval was consumed; the remote file really exists.
            assert (
                runtime2.approvals.get(approval_tool.approval_id).status
                is ApprovalStatus.CONSUMED
            )
            executed_tool = runtime2.investigation_store.get_tool_call(
                approval_tool.tool_call_id
            )
            assert executed_tool.status is ToolCallStatus.SUCCEEDED
            assert executed_tool.evidence_ids
            assert await _remote_file_exists(live_target, APPROVAL_MARKER)
            # Resume ran from the latest checkpoint: round 2 executed once (not
            # replayed), so checkpoints are 1-4 and rounds are exactly 1-2.
            resumed_checkpoints = [
                cp.sequence
                for cp in runtime2.investigation_store.list_checkpoints(parent_b_id)
            ]
            assert resumed_checkpoints == [1, 2, 3, 4]
            resumed_rounds = [
                r.round_number
                for r in runtime2.investigation_store.list_rounds(parent_b_id)
            ]
            assert resumed_rounds == [1, 2]

            # ---- Scenario C: uncertain in-flight call is never replayed. ------
            runtime3 = _build_runtime(tmp_path, live_target, registry)
            uncertain_run = "live-uncertain-parent"
            _seed_in_flight_shell(
                runtime3, "inv-live-c", uncertain_run, "c-shell"
            )
            await runtime3.recovery.startup()
            parked_uncertain = runtime3.investigations.get_run(uncertain_run)
            assert parked_uncertain.status is AgentRunStatus.PAUSED_UNCERTAIN_STATE
            assert parked_uncertain.stop_reason is StopReason.UNCERTAIN_STATE
            uncertain_call = runtime3.investigation_store.get_tool_call("c-shell")
            assert uncertain_call.status is ToolCallStatus.UNCERTAIN
            assert "never replayed" in (uncertain_call.error_redacted or "")
            uncertain_evidence = runtime3.evidence.query(
                agent_run_id=uncertain_run, evidence_kind=EvidenceKind.UNCERTAIN_STATE
            )
            assert uncertain_evidence

            # Resume with a fresh script; the UNCERTAIN dangerous call is never
            # re-executed, and the run completes on new evidence.
            registry.set_script(
                uncertain_run,
                [
                    RequestToolsStep(
                        tool_requests=(
                            _tool(
                                "log_query",
                                "c-log",
                                service_name=SERVICE,
                                source_kind="file",
                                source_ref=str(LOG_FILE),
                                tail_lines=100,
                            ),
                        )
                    ),
                    StopStep(
                        stop_signal=StopSignal(
                            stop_reason=StopReason.COMPLETED, summary="uncertain done"
                        )
                    ),
                ],
            )
            final_c = await asyncio.wait_for(
                runtime3.investigations.resume_run(uncertain_run), timeout=60.0
            )
            assert final_c.status is AgentRunStatus.COMPLETED
            replayed = runtime3.investigation_store.get_tool_call("c-shell")
            assert replayed.status is ToolCallStatus.UNCERTAIN
            assert replayed.tool_call_id == "c-shell"
            # Only the resume's log call exists beyond the parked UNCERTAIN one.
            all_calls = runtime3.investigation_store.list_tool_calls(
                agent_run_id=uncertain_run
            )
            assert len(all_calls) == 2
            assert [call.tool_name for call in all_calls] == ["shell_exec", "log_query"]
            assert [call.status for call in all_calls] == [
                ToolCallStatus.UNCERTAIN,
                ToolCallStatus.SUCCEEDED,
            ]
        finally:
            await runtime1.sessions.close_all()
            if runtime2 is not None:
                await runtime2.sessions.close_all()
            if runtime3 is not None:
                await runtime3.sessions.close_all()

    asyncio.run(scenario())
