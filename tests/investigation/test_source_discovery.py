"""Tests for dynamic source discovery and approval-gated registry updates.

The harness wires a real SQLite-backed project registry, evidence store,
investigation store, approval store, event store and change manager around a
``SessionManager`` over a scripted in-memory transport that simulates the fixed
``docker ps`` / ``docker inspect`` / ``docker compose config`` argv templates and
host file operations — every test walks the real evidence / approval / atomic
writeback paths without touching the network.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.approvals.types import ApprovalStatus
from incidentlens_control_plane.changes.backup import EncryptedBackupVault
from incidentlens_control_plane.changes.manager import ChangeManager
from incidentlens_control_plane.changes.store import ChangeSetStore
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEventType
from incidentlens_control_plane.evidence.service import EvidenceService
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.evidence.types import EvidenceKind
from incidentlens_control_plane.investigation.registry_proposals import (
    RegistryProposalError,
    RegistryProposalService,
    registry_update_intent,
)
from incidentlens_control_plane.investigation.source_discovery import (
    DiscoveryCandidateKind,
    SourceDiscoveryError,
    SourceDiscoveryService,
)
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
)
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    Investigation,
    InvestigationBudget,
    RegistryProposalStatus,
    RegistryUpdateKind,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.gateway import RemoteToolGateway
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.transport import (
    CommandResult,
    FileMetadata,
    RemotePathError,
)

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)

PROJECT_ID = "payments"
TARGET_ID = "dev-a"
SERVICE = "payment-api"
CONTAINER = "payments-api-1"
HOST_ROOT = PurePosixPath("/opt/payments")
LOG_PATH = PurePosixPath("/var/log/payment/app.log")
LOG_ROOT = LOG_PATH.parent
CONTAINER_ROOT = PurePosixPath("/app")


def _project_registration() -> ProjectRegistration:
    return ProjectRegistration(
        project_id=PROJECT_ID,
        display_name="Payments",
        local_source_paths=(Path("/srv/payments"),),
        targets=(
            TargetRegistration(
                target_id=TARGET_ID,
                host="dev-a.example.test",
                ssh_user="deploy",
                ssh_config_alias="dev-a",
                compose_working_directory=PurePosixPath("/opt/payments"),
                compose_project_name="payments",
            ),
        ),
        services=(
            ServiceRegistration(
                compose_service=SERVICE,
                container_names=(CONTAINER, "payments-api-2"),
                allowed_log_paths=(str(LOG_PATH),),
                allowed_host_paths=(HOST_ROOT, LOG_ROOT),
                allowed_container_paths=(CONTAINER_ROOT,),
                container_path_hints=("/app/logs",),
            ),
        ),
    )


def make_scope(
    *,
    scope_kind: LogScope = LogScope.HOST,
) -> AgentScope:
    if scope_kind is LogScope.CONTAINER:
        return AgentScope(
            project_id=PROJECT_ID,
            target_id=TARGET_ID,
            scope=LogScope.CONTAINER,
            service_name=SERVICE,
            container_name=CONTAINER,
            allowed_host_paths=(HOST_ROOT, LOG_ROOT),
            allowed_container_paths=(CONTAINER_ROOT,),
        )
    return AgentScope(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        scope=LogScope.HOST,
        allowed_host_paths=(HOST_ROOT, LOG_ROOT),
        allowed_container_paths=(CONTAINER_ROOT,),
    )


def _new_run(
    investigations: InvestigationStore,
    *,
    run_id: str = "run-1",
    scope: AgentScope | None = None,
    incident_id: str = "inc-1",
) -> AgentRun:
    scope = scope or make_scope()
    investigations.create_investigation(
        Investigation(
            investigation_id="inv-1",
            incident_id=incident_id,
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
        budget=AgentBudget(),
        usage=UsageCounters(),
        created_at=NOW,
        updated_at=NOW,
    )
    investigations.create_agent_run(run)
    return run


# ---------------------------------------------------------------------------
# Scripted in-memory transport simulating the fixed docker read argv templates
# ---------------------------------------------------------------------------


class DiscoveryTransport:
    """A scripted transport: host files plus docker ps/inspect/compose config.

    ``run_argv`` only answers the fixed read-only argv templates the discovery
    and identity-verification code sends: ``docker ps --format {{.Names}}``,
    ``docker inspect <container>``, ``docker inspect --format {{.Id}}
    <container>`` and ``docker compose ... config``.  Anything else returns an
    empty success so no unrelated command is ever simulated.
    """

    def __init__(self, target: TargetRegistration) -> None:
        self.target = target
        self.host_files: dict[PurePosixPath, bytes] = {}
        self.host_dirs: set[PurePosixPath] = set()
        self.running_containers: list[str] = []
        self.inspect_outputs: dict[str, dict[str, Any]] = {}
        self.compose_config: bytes = b"{}"
        self.missing_paths: set[PurePosixPath] = set()
        self.run_argv_calls: list[tuple[str, ...]] = []
        self.closed = False

    async def is_alive(self) -> bool:
        return not self.closed

    async def realpath(self, path: PurePosixPath) -> PurePosixPath:
        if path in self.missing_paths:
            raise RemotePathError(f"path does not exist: {path}")
        return path

    async def lstat(self, path: PurePosixPath) -> FileMetadata:
        if path in self.missing_paths:
            raise RemotePathError(f"path does not exist: {path}")
        if path in self.host_dirs:
            return FileMetadata(
                path=path, size=0, mode=0o40755, uid=1000, gid=1000,
                modified_ns=0, is_symlink=False,
            )
        data = self.host_files.get(path, b"")
        return FileMetadata(
            path=path, size=len(data), mode=0o100644, uid=1000, gid=1000,
            modified_ns=0, is_symlink=False,
        )

    async def read_bytes(
        self, path: PurePosixPath, *, offset: int = 0, max_bytes: int
    ) -> bytes:
        data = self.host_files.get(path, b"")
        return data[offset : offset + max_bytes]

    async def list_directory(self, path: PurePosixPath) -> tuple[FileMetadata, ...]:
        entries: list[FileMetadata] = []
        for file_path, data in self.host_files.items():
            if file_path.parent == path:
                entries.append(
                    FileMetadata(
                        path=file_path, size=len(data), mode=0o100644, uid=1000,
                        gid=1000, modified_ns=0, is_symlink=False,
                    )
                )
        for dir_path in self.host_dirs:
            if dir_path.parent == path:
                entries.append(
                    FileMetadata(
                        path=dir_path, size=0, mode=0o40755, uid=1000, gid=1000,
                        modified_ns=0, is_symlink=False,
                    )
                )
        return tuple(entries)

    async def write_bytes(
        self, path: PurePosixPath, content: bytes, *, mode: int = 0o644,
        exclusive: bool = False,
    ) -> None:
        self.host_files[path] = content

    async def rename(self, source: PurePosixPath, target: PurePosixPath) -> None:
        if source in self.host_files:
            self.host_files[target] = self.host_files.pop(source)

    async def remove_file(self, path: PurePosixPath) -> None:
        self.host_files.pop(path, None)

    async def copy_file(
        self, source: PurePosixPath, target: PurePosixPath, *, preserve: bool = True
    ) -> None:
        if source in self.host_files:
            self.host_files[target] = self.host_files[source]

    async def run_argv(
        self, argv: tuple[str, ...], *, timeout: float = 30.0
    ) -> CommandResult:
        self.run_argv_calls.append(argv)
        if argv[:4] == ("docker", "ps", "--format", "{{.Names}}"):
            names = "\n".join(self.running_containers)
            return CommandResult(
                exit_status=0,
                stdout=(names + "\n").encode() if names else b"",
                stderr=b"",
            )
        if argv[:4] == ("docker", "inspect", "--format", "{{.Id}}"):
            container = argv[4]
            if container in self.inspect_outputs:
                return CommandResult(exit_status=0, stdout=b"sha256:deadbeef", stderr=b"")
            return CommandResult(exit_status=1, stdout=b"", stderr=b"no such container")
        if argv[:2] == ("docker", "inspect") and len(argv) == 3:
            container = argv[2]
            if container in self.inspect_outputs:
                return CommandResult(
                    exit_status=0,
                    stdout=json.dumps(self.inspect_outputs[container]).encode(),
                    stderr=b"",
                )
            return CommandResult(exit_status=1, stdout=b"", stderr=b"no such container")
        if "compose" in argv and argv[-1] == "config":
            return CommandResult(exit_status=0, stdout=self.compose_config, stderr=b"")
        return CommandResult(exit_status=0, stdout=b"", stderr=b"")

    async def open_shell(self) -> Any:
        from incidentlens_control_plane.remote_ops.fakes import FakeProcess

        return FakeProcess()

    async def open_process(self, argv: tuple[str, ...], *, term_type: str | None) -> Any:
        from incidentlens_control_plane.remote_ops.fakes import FakeProcess

        return FakeProcess()

    async def close(self) -> None:
        self.closed = True


class DiscoveryTransportFactory:
    """Returns one live ``DiscoveryTransport`` per target, mirroring SessionManager."""

    def __init__(self, transport: DiscoveryTransport | None = None) -> None:
        self._transport = transport
        self._live: dict[str, DiscoveryTransport] = {}
        self.transports: list[DiscoveryTransport] = []

    async def connect(self, target: TargetRegistration) -> DiscoveryTransport:
        if self._transport is not None:
            return self._transport
        existing = self._live.get(target.target_id)
        if existing is not None:
            return existing
        transport = DiscoveryTransport(target)
        self._live[target.target_id] = transport
        self.transports.append(transport)
        return transport


@dataclass
class Harness:
    projects: ProjectRegistryStore
    sessions: SessionManager
    gateway: RemoteToolGateway
    evidence: EvidenceService
    evidence_store: EvidenceStore
    events: RuntimeEventStore
    broker: RuntimeEventBroker
    investigations: InvestigationStore
    approvals: ApprovalService
    discovery: SourceDiscoveryService
    proposals: RegistryProposalService
    transport: DiscoveryTransport


def build_harness(
    tmp_path: Path,
    *,
    transport: DiscoveryTransport | None = None,
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
    evidence_store = EvidenceStore(connect)
    investigations = InvestigationStore(connect)
    for store in (
        projects,
        events,
        approval_store,
        change_store,
        evidence_store,
        investigations,
    ):
        store.migrate()

    projects.create(_project_registration(), now=NOW)
    target = projects.get(PROJECT_ID).targets[0]

    broker = RuntimeEventBroker()
    approvals = ApprovalService(approvals=approval_store, events=events, broker=broker)
    if transport is None:
        transport = DiscoveryTransport(target)
    factory = DiscoveryTransportFactory(transport)
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
    evidence = EvidenceService(evidence_store, investigations=investigations)
    discovery = SourceDiscoveryService(
        projects=projects,
        gateway=gateway,
        sessions=sessions,
        evidence=evidence,
        investigations=investigations,
    )
    proposals = RegistryProposalService(
        projects=projects,
        investigations=investigations,
        approvals=approvals,
        evidence=evidence,
        events=events,
        broker=broker,
        gateway=gateway,
        sessions=sessions,
    )
    return Harness(
        projects=projects,
        sessions=sessions,
        gateway=gateway,
        evidence=evidence,
        evidence_store=evidence_store,
        events=events,
        broker=broker,
        investigations=investigations,
        approvals=approvals,
        discovery=discovery,
        proposals=proposals,
        transport=transport,
    )


def _inspect_document(*, mounts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "Id": "sha256:" + "a" * 64,
        "Image": "sha256:" + "b" * 64,
        "RepoDigests": ["payments/api@sha256:" + "c" * 64],
        "Config": {
            "Labels": {"com.example.owner": "payments"},
            "WorkingDir": "/app",
            "Entrypoint": ["/usr/bin/api"],
            "Cmd": ["serve", "--port", "8080"],
        },
        "Mounts": mounts or [],
    }


def _ghost_transport(*, container: str = "ghost-worker-1") -> DiscoveryTransport:
    """A transport where *container* is running and inspectable."""
    transport = DiscoveryTransport(
        TargetRegistration(target_id=TARGET_ID, host="h", ssh_user="u")
    )
    transport.running_containers = [CONTAINER, container]
    transport.inspect_outputs[container] = _inspect_document()
    transport.inspect_outputs[CONTAINER] = _inspect_document()
    return transport


# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_service_discovery_surfaces_unregistered_running_container(
    tmp_path: Path,
) -> None:
    transport = DiscoveryTransport(TargetRegistration(target_id=TARGET_ID, host="h", ssh_user="u"))
    transport.running_containers = [CONTAINER, "ghost-worker-1"]
    transport.inspect_outputs[CONTAINER] = _inspect_document()
    harness = build_harness(tmp_path, transport=transport)
    run = _new_run(harness.investigations)

    outcome = await harness.discovery.discover(run, service_name=SERVICE, now=NOW)

    assert any(
        candidate.kind is DiscoveryCandidateKind.CONTAINER
        and candidate.name == "ghost-worker-1"
        for candidate in outcome.candidates
    )
    assert not any(
        candidate.name == CONTAINER for candidate in outcome.candidates
    )
    # The unregistered container was never inspected.
    inspected = [
        argv[2]
        for argv in transport.run_argv_calls
        if argv[:2] == ("docker", "inspect") and len(argv) == 3
    ]
    assert "ghost-worker-1" not in inspected
    # The registered container WAS inspected and its config recorded.
    assert "payments-api-1" in inspected
    stored_kinds = {
        harness.evidence_store.get(ref.evidence_id).evidence_kind
        for ref in outcome.evidence
    }
    assert EvidenceKind.REGISTRY_DISCOVERY in stored_kinds
    assert EvidenceKind.COMMAND_OUTPUT in stored_kinds


@pytest.mark.asyncio
async def test_container_inspect_records_image_digest_labels_workdir(tmp_path: Path) -> None:
    transport = DiscoveryTransport(TargetRegistration(target_id=TARGET_ID, host="h", ssh_user="u"))
    transport.inspect_outputs[CONTAINER] = _inspect_document()
    harness = build_harness(tmp_path, transport=transport)
    run = _new_run(harness.investigations)

    outcome = await harness.discovery.discover(
        run, service_name=SERVICE, container=CONTAINER, now=NOW
    )

    assert outcome.candidates == ()
    summaries = " ".join(ref.summary for ref in outcome.evidence)
    assert "digest=" in summaries
    assert "workdir=" in summaries
    assert "start=" in summaries
    config_ref = next(
        harness.evidence_store.get(ref.evidence_id)
        for ref in outcome.evidence
        if harness.evidence_store.get(ref.evidence_id).evidence_kind
        is EvidenceKind.REGISTRY_DISCOVERY
    )
    assert "container=payments-api-1" in config_ref.content_redacted


@pytest.mark.asyncio
async def test_container_inspect_mount_outside_scope_becomes_path_candidate(
    tmp_path: Path,
) -> None:
    transport = DiscoveryTransport(TargetRegistration(target_id=TARGET_ID, host="h", ssh_user="u"))
    transport.inspect_outputs[CONTAINER] = _inspect_document(
        mounts=[
            {"Type": "bind", "Source": "/opt/payments", "Destination": "/app"},
            {"Type": "bind", "Source": "/srv/ghost-data", "Destination": "/data"},
            {"Type": "volume", "Name": "db-vol", "Destination": "/db"},
        ]
    )
    harness = build_harness(tmp_path, transport=transport)
    run = _new_run(harness.investigations)

    outcome = await harness.discovery.discover(
        run, service_name=SERVICE, container=CONTAINER, now=NOW
    )

    path_candidates = [
        candidate
        for candidate in outcome.candidates
        if candidate.kind is DiscoveryCandidateKind.HOST_PATH
    ]
    assert any(candidate.name == "/srv/ghost-data" for candidate in path_candidates)
    # /opt/payments is a registered allowed path; named volumes have no host path.
    assert not any(candidate.name == "/opt/payments" for candidate in path_candidates)
    assert not any(candidate.name == "/db" for candidate in path_candidates)
    # No directory listing or read ever touched the candidate path.
    touched = {call for call in transport.run_argv_calls}
    assert not any("/srv/ghost-data" in " ".join(call) for call in touched)


@pytest.mark.asyncio
async def test_host_path_discovery_records_file_types_and_source_hashes(
    tmp_path: Path,
) -> None:
    transport = DiscoveryTransport(TargetRegistration(target_id=TARGET_ID, host="h", ssh_user="u"))
    transport.host_files[HOST_ROOT / "app.py"] = b"print('ok')\n"
    transport.host_files[HOST_ROOT / "requirements.txt"] = b"flask==2.0\n"
    transport.host_dirs = {HOST_ROOT / "templates"}
    harness = build_harness(tmp_path, transport=transport)
    run = _new_run(harness.investigations)

    outcome = await harness.discovery.discover(
        run, service_name=SERVICE, path=str(HOST_ROOT), now=NOW
    )

    assert outcome.candidates == ()
    summaries = " ".join(ref.summary for ref in outcome.evidence)
    assert "host path /opt/payments" in summaries
    assert "sha256=" in summaries
    # The listing records file types.
    listing = next(
        ref
        for ref in outcome.evidence
        if "host path /opt/payments" in ref.summary
    )
    stored = harness.evidence_store.get(listing.evidence_id)
    assert "type=file" in stored.content_redacted
    assert "type=directory" in stored.content_redacted
    # Source hashes are recorded as a registry discovery summary.
    hash_summaries = [
        ref for ref in outcome.evidence if "sha256=" in ref.summary
    ]
    assert hash_summaries


@pytest.mark.asyncio
async def test_container_run_cannot_enumerate_host_docker_or_paths(tmp_path: Path) -> None:
    transport = DiscoveryTransport(TargetRegistration(target_id=TARGET_ID, host="h", ssh_user="u"))
    transport.running_containers = [CONTAINER, "ghost-worker-1"]
    transport.inspect_outputs[CONTAINER] = _inspect_document()
    harness = build_harness(tmp_path, transport=transport)
    run = _new_run(
        harness.investigations, scope=make_scope(scope_kind=LogScope.CONTAINER)
    )

    with pytest.raises(SourceDiscoveryError):
        await harness.discovery.discover(
            run, service_name=SERVICE, path=str(HOST_ROOT), now=NOW
        )
    assert transport.run_argv_calls == []

    outcome = await harness.discovery.discover(
        run, service_name=SERVICE, now=NOW
    )
    # A container run never runs host-level docker ps / inspect.
    assert not any(call[:2] == ("docker", "ps") for call in transport.run_argv_calls)
    assert not any(call[:2] == ("docker", "inspect") for call in transport.run_argv_calls)
    assert outcome.candidates == ()


@pytest.mark.asyncio
async def test_direct_unregistered_container_and_path_rejected(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)

    with pytest.raises(SourceDiscoveryError):
        await harness.discovery.discover(
            run, service_name=SERVICE, container="ghost-container", now=NOW
        )
    with pytest.raises(SourceDiscoveryError):
        await harness.discovery.discover(
            run, service_name=SERVICE, path="/etc/passwd", now=NOW
        )
    with pytest.raises(SourceDiscoveryError):
        await harness.discovery.discover(
            run, service_name="ghost-service", now=NOW
        )


# ---------------------------------------------------------------------------
# Registry update proposals
# ---------------------------------------------------------------------------


def _approval_for(harness: Harness, approval_id: str):
    for approval in harness.approvals.list():
        if approval.approval_id == approval_id:
            return approval
    raise AssertionError(f"approval {approval_id} not found")


@pytest.mark.asyncio
async def test_propose_persists_proposal_and_requests_exact_intent(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)

    outcome = await harness.proposals.propose(
        run,
        discovery_evidence_id="ev-discovery-1",
        kind=RegistryUpdateKind.CONTAINER_REGISTRATION,
        service_name=SERVICE,
        container="ghost-worker-1",
        now=NOW,
    )

    proposal = harness.investigations.get_proposal(outcome.proposal.proposal_id)
    assert proposal.status is RegistryProposalStatus.PENDING
    assert proposal.proposed_container_name == "ghost-worker-1"
    assert proposal.discovery_evidence_id == "ev-discovery-1"
    assert outcome.approval.status is ApprovalStatus.PENDING
    assert proposal.approval_intent_sha256 == outcome.approval.intent_sha256

    expected = {
        "kind": "registry_update",
        "update_kind": "container_registration",
        "project_id": PROJECT_ID,
        "target_id": TARGET_ID,
        "service": SERVICE,
        "container": "ghost-worker-1",
    }
    assert registry_update_intent(proposal) == expected
    assert outcome.approval.intent == expected
    # A registry proposal evidence reference backs the pending proposal.
    assert len(outcome.evidence) == 1


@pytest.mark.asyncio
async def test_approved_container_registration_writes_back_and_consumes(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path, transport=_ghost_transport())
    run = _new_run(harness.investigations)
    proposed = await harness.proposals.propose(
        run,
        discovery_evidence_id="ev-discovery-1",
        kind=RegistryUpdateKind.CONTAINER_REGISTRATION,
        service_name=SERVICE,
        container="ghost-worker-1",
        now=NOW,
    )
    await harness.approvals.approve(proposed.approval.approval_id)
    approval = _approval_for(harness, proposed.approval.approval_id)

    decided = await harness.proposals.handle_approval_decision(
        proposed.proposal, approval, now=NOW
    )

    assert decided.applied is True
    assert decided.decision == "approved"
    assert decided.record is not None
    service = next(
        s for s in decided.record.services if s.compose_service == SERVICE
    )
    assert "ghost-worker-1" in service.container_names
    # The exact single-use approval was consumed.
    assert _approval_for(harness, proposed.approval.approval_id).status is ApprovalStatus.CONSUMED
    # The proposal moved to APPROVED with a decision evidence ref.
    stored_proposal = harness.investigations.get_proposal(proposed.proposal.proposal_id)
    assert stored_proposal.status is RegistryProposalStatus.APPROVED
    assert decided.evidence
    stored = harness.evidence_store.get(decided.evidence[0].evidence_id)
    assert stored.evidence_kind is EvidenceKind.APPROVAL_DECISION
    assert stored.metadata["decision"] == "approved"
    # An audit event was emitted for the writeback.
    audit_events = [
        event for event in harness.events.list_after(0)
        if event.event_type is RuntimeEventType.PROJECT_UPDATED
    ]
    assert any(
        event.payload.get("proposal_id") == proposed.proposal.proposal_id
        for event in audit_events
    )


@pytest.mark.asyncio
async def test_path_extension_applies_canonical_paths(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    proposed = await harness.proposals.propose(
        run,
        discovery_evidence_id="ev-discovery-2",
        kind=RegistryUpdateKind.PATH_EXTENSION,
        service_name=SERVICE,
        paths=(PurePosixPath("/srv/ghost-data"),),
        now=NOW,
    )
    await harness.approvals.approve(proposed.approval.approval_id)
    approval = _approval_for(harness, proposed.approval.approval_id)

    decided = await harness.proposals.handle_approval_decision(
        proposed.proposal, approval, now=NOW
    )

    assert decided.applied is True
    service = next(
        s for s in decided.record.services if s.compose_service == SERVICE
    )
    assert PurePosixPath("/srv/ghost-data") in service.allowed_host_paths
    assert _approval_for(harness, proposed.approval.approval_id).status is ApprovalStatus.CONSUMED


@pytest.mark.asyncio
async def test_rejected_approval_returns_evidence_and_leaves_registry_untouched(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    proposed = await harness.proposals.propose(
        run,
        discovery_evidence_id="ev-discovery-3",
        kind=RegistryUpdateKind.CONTAINER_REGISTRATION,
        service_name=SERVICE,
        container="ghost-worker-1",
        now=NOW,
    )
    await harness.approvals.reject(proposed.approval.approval_id)
    approval = _approval_for(harness, proposed.approval.approval_id)

    decided = await harness.proposals.handle_approval_decision(
        proposed.proposal, approval, now=NOW
    )

    assert decided.applied is False
    assert decided.decision == "rejected"
    service = next(
        s for s in harness.projects.get(PROJECT_ID).services if s.compose_service == SERVICE
    )
    assert "ghost-worker-1" not in service.container_names
    stored_proposal = harness.investigations.get_proposal(proposed.proposal.proposal_id)
    assert stored_proposal.status is RegistryProposalStatus.REJECTED
    stored = harness.evidence_store.get(decided.evidence[0].evidence_id)
    assert stored.evidence_kind is EvidenceKind.APPROVAL_DECISION
    assert stored.metadata["decision"] == "rejected"


@pytest.mark.asyncio
async def test_approval_for_already_registered_container_is_stale(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)
    proposed = await harness.proposals.propose(
        run,
        discovery_evidence_id="ev-discovery-4",
        kind=RegistryUpdateKind.CONTAINER_REGISTRATION,
        service_name=SERVICE,
        container=CONTAINER,  # already registered
        now=NOW,
    )
    await harness.approvals.approve(proposed.approval.approval_id)
    approval = _approval_for(harness, proposed.approval.approval_id)

    decided = await harness.proposals.handle_approval_decision(
        proposed.proposal, approval, now=NOW
    )

    assert decided.applied is False
    assert decided.decision == "stale"
    stored_proposal = harness.investigations.get_proposal(proposed.proposal.proposal_id)
    assert stored_proposal.status is RegistryProposalStatus.STALE
    # The single-use approval is consumed so it cannot be replayed.
    assert _approval_for(harness, proposed.approval.approval_id).status is ApprovalStatus.CONSUMED


@pytest.mark.asyncio
async def test_approval_for_missing_container_identity_is_stale(tmp_path: Path) -> None:
    transport = DiscoveryTransport(TargetRegistration(target_id=TARGET_ID, host="h", ssh_user="u"))
    # No inspect output for ghost-worker-1 -> docker inspect returns non-zero.
    harness = build_harness(tmp_path, transport=transport)
    run = _new_run(harness.investigations)
    proposed = await harness.proposals.propose(
        run,
        discovery_evidence_id="ev-discovery-5",
        kind=RegistryUpdateKind.CONTAINER_REGISTRATION,
        service_name=SERVICE,
        container="ghost-worker-1",
        now=NOW,
    )
    await harness.approvals.approve(proposed.approval.approval_id)
    approval = _approval_for(harness, proposed.approval.approval_id)

    decided = await harness.proposals.handle_approval_decision(
        proposed.proposal, approval, now=NOW
    )

    assert decided.applied is False
    assert decided.decision == "stale"
    assert "not running" in decided.reason
    service = next(
        s for s in harness.projects.get(PROJECT_ID).services if s.compose_service == SERVICE
    )
    assert "ghost-worker-1" not in service.container_names


@pytest.mark.asyncio
async def test_path_extension_with_missing_path_is_stale(tmp_path: Path) -> None:
    transport = DiscoveryTransport(TargetRegistration(target_id=TARGET_ID, host="h", ssh_user="u"))
    transport.missing_paths = {PurePosixPath("/srv/ghost-data")}
    harness = build_harness(tmp_path, transport=transport)
    run = _new_run(harness.investigations)
    proposed = await harness.proposals.propose(
        run,
        discovery_evidence_id="ev-discovery-6",
        kind=RegistryUpdateKind.PATH_EXTENSION,
        service_name=SERVICE,
        paths=(PurePosixPath("/srv/ghost-data"),),
        now=NOW,
    )
    await harness.approvals.approve(proposed.approval.approval_id)
    approval = _approval_for(harness, proposed.approval.approval_id)

    decided = await harness.proposals.handle_approval_decision(
        proposed.proposal, approval, now=NOW
    )

    assert decided.applied is False
    assert decided.decision == "stale"
    service = next(
        s for s in harness.projects.get(PROJECT_ID).services if s.compose_service == SERVICE
    )
    assert PurePosixPath("/srv/ghost-data") not in service.allowed_host_paths


@pytest.mark.asyncio
async def test_second_decision_is_a_no_op(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, transport=_ghost_transport())
    run = _new_run(harness.investigations)
    proposed = await harness.proposals.propose(
        run,
        discovery_evidence_id="ev-discovery-7",
        kind=RegistryUpdateKind.CONTAINER_REGISTRATION,
        service_name=SERVICE,
        container="ghost-worker-1",
        now=NOW,
    )
    await harness.approvals.approve(proposed.approval.approval_id)
    approval = _approval_for(harness, proposed.approval.approval_id)
    first = await harness.proposals.handle_approval_decision(
        proposed.proposal, approval, now=NOW
    )
    second = await harness.proposals.handle_approval_decision(
        proposed.proposal, approval, now=NOW
    )

    assert first.applied is True
    assert second.applied is False
    assert second.decision == "approved"
    assert "already decided" in second.reason


@pytest.mark.asyncio
async def test_propose_rejects_incomplete_updates(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)

    with pytest.raises(RegistryProposalError):
        await harness.proposals.propose(
            run,
            discovery_evidence_id="ev-x",
            kind=RegistryUpdateKind.CONTAINER_REGISTRATION,
            service_name=SERVICE,
            now=NOW,
        )
    with pytest.raises(RegistryProposalError):
        await harness.proposals.propose(
            run,
            discovery_evidence_id="ev-x",
            kind=RegistryUpdateKind.PATH_EXTENSION,
            service_name=SERVICE,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_path_extension_intent_has_no_container_key(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    run = _new_run(harness.investigations)

    outcome = await harness.proposals.propose(
        run,
        discovery_evidence_id="ev-shape",
        kind=RegistryUpdateKind.PATH_EXTENSION,
        service_name=SERVICE,
        paths=(PurePosixPath("/srv/ghost-data"),),
        now=NOW,
    )

    intent = registry_update_intent(outcome.proposal)
    assert intent["update_kind"] == "path_extension"
    assert intent["paths"] == ["/srv/ghost-data"]
    assert "container" not in intent
