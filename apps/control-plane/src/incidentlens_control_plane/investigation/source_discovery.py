"""Dynamic source discovery inside registered bounds.

``SourceDiscoveryService`` collects typed, redacted evidence about a registered
service, container or host path using the existing ``RemoteToolGateway`` file
ops (``read`` / ``list_dir`` / ``stat``) and fixed read-only ``docker`` argv
templates (``ps`` / ``inspect`` / ``compose config``) — there is no second
remote execution channel.  Every result is persisted through
``EvidenceService`` (``registry_discovery``, ``command_output``,
``file_snapshot``) before it can back a registry proposal, so the model only
ever sees evidence ids plus a bounded summary.

When authorized output exposes a container or host path that is NOT registered
for the service, discovery stops short of accessing that candidate and
surfaces it in the ``DiscoveryOutcome``, so ``RegistryProposalService`` can
back it with the exact evidence that exposed it.  A container-scoped run is
pinned to its own container: host-level docker discovery (``docker ps`` /
``docker inspect``) is never performed for it, and only paths inside its own
``allowed_container_paths`` are inspected.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from incidentlens_control_plane.evidence.service import (
    CONTENT_MAX_LENGTH,
    EvidenceService,
)
from incidentlens_control_plane.evidence.types import EvidenceRef
from incidentlens_control_plane.investigation.store import (
    InvestigationNotFound,
    InvestigationStore,
)
from incidentlens_control_plane.investigation.types import (
    AgentRun,
    EvidenceReference,
)
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import (
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.gateway import RemoteToolGateway
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.transport import (
    RemotePathError,
    RemoteTimeoutError,
)

_MAX_REGISTERED_CONTAINERS = 8
_MAX_HOST_PATHS = 8
_MAX_LISTING_ENTRIES = 100
_MAX_SOURCE_FILES = 8
_MAX_SOURCE_FILE_BYTES = 64 * 1024
_DOCKER_TIMEOUT = 30.0


class SourceDiscoveryError(Exception):
    """A deterministic discovery/validation failure safe to report to the model."""


class DiscoveryCandidateKind(StrEnum):
    CONTAINER = "container"
    HOST_PATH = "host_path"


@dataclass(frozen=True)
class DiscoveryCandidate:
    """A container or path that authorized output exposed but is not registered.

    The candidate is deliberately never accessed: the evidence id records the
    discovery that exposed it so ``RegistryProposalService`` can back an exact
    proposal without widening access before approval.
    """

    kind: DiscoveryCandidateKind
    name: str
    service_name: str
    evidence_id: str
    summary: str


@dataclass(frozen=True)
class DiscoveryOutcome:
    """The bounded result of one discovery run: evidence plus candidates."""

    service_name: str
    evidence: tuple[EvidenceReference, ...]
    candidates: tuple[DiscoveryCandidate, ...]
    summary: str


class SourceDiscoveryService:
    """Collect typed discovery evidence within a run's registered bounds."""

    def __init__(
        self,
        *,
        projects: ProjectRegistryStore,
        gateway: RemoteToolGateway,
        sessions: SessionManager,
        evidence: EvidenceService,
        investigations: InvestigationStore,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._projects = projects
        self._gateway = gateway
        self._sessions = sessions
        self._evidence = evidence
        self._investigations = investigations
        self._now = now or (lambda: datetime.now(UTC))

    # -- public entry points --------------------------------------------------

    async def discover(
        self,
        run: AgentRun,
        *,
        service_name: str,
        container: str | None = None,
        path: str | None = None,
        now: datetime | None = None,
    ) -> DiscoveryOutcome:
        """Discover a registered service, container or host path in *run* scope."""
        svc = self._resolve_service(run, service_name)
        now = now or self._now()
        evidence_refs: list[EvidenceReference] = []
        candidates: list[DiscoveryCandidate] = []

        if container is not None:
            container = self._validate_container(run, svc, container)
            if run.scope.scope is LogScope.CONTAINER:
                await self._discover_container_paths(
                    run, svc, container, now, evidence_refs, candidates
                )
            else:
                await self._discover_container(
                    run, svc, container, now, evidence_refs, candidates
                )
            return self._outcome(service_name, evidence_refs, candidates)

        if path is not None:
            self._assert_run_scope(run, LogScope.HOST)
            parsed = self._parse_path(path)
            self._validate_path_in_scope(run, parsed, scope=LogScope.HOST)
            await self._discover_host_path(
                run, svc, parsed, now, evidence_refs, candidates
            )
            return self._outcome(service_name, evidence_refs, candidates)

        return await self._discover_service(run, svc, now)

    # -- service-level discovery ----------------------------------------------

    async def _discover_service(
        self,
        run: AgentRun,
        svc: ServiceRegistration,
        now: datetime,
    ) -> DiscoveryOutcome:
        evidence_refs: list[EvidenceReference] = []
        candidates: list[DiscoveryCandidate] = []

        if run.scope.scope is LogScope.CONTAINER:
            # A container run is pinned to its own container and cannot reach
            # host-level docker state.
            container = self._validate_container(run, svc, run.scope.container_name)
            await self._discover_container_paths(
                run, svc, container, now, evidence_refs, candidates
            )
        else:
            # docker ps exposes every running container; unregistered ones are
            # candidates that must never be inspected.
            ps_output = await self._run_docker_read(
                run,
                svc,
                ("docker", "ps", "--format", "{{.Names}}"),
                source_ref="docker:ps",
                description="running containers",
                now=now,
                evidence_refs=evidence_refs,
            )
            if ps_output is not None:
                self._collect_container_candidates(
                    run, svc, ps_output, now, evidence_refs, candidates
                )
            await self._discover_compose_config(run, svc, now, evidence_refs)
            for name in svc.container_names[:_MAX_REGISTERED_CONTAINERS]:
                await self._discover_container(
                    run, svc, name, now, evidence_refs, candidates
                )
            for host_path in svc.allowed_host_paths[:_MAX_HOST_PATHS]:
                if self._path_allowed(run, host_path, scope=LogScope.HOST):
                    await self._discover_host_path(
                        run, svc, host_path, now, evidence_refs, candidates
                    )

        summary = (
            f"discovered {len(evidence_refs)} evidence item(s), "
            f"{len(candidates)} unregistered candidate(s) for {svc.compose_service}"
        )
        return DiscoveryOutcome(
            service_name=svc.compose_service,
            evidence=tuple(evidence_refs),
            candidates=tuple(candidates),
            summary=summary,
        )

    async def _discover_compose_config(
        self,
        run: AgentRun,
        svc: ServiceRegistration,
        now: datetime,
        evidence_refs: list[EvidenceReference],
    ) -> None:
        target = self._resolve_target(run, svc)
        if target.compose_working_directory is None:
            return
        argv = [
            "docker",
            "compose",
            "--project-directory",
            str(target.compose_working_directory),
        ]
        if target.compose_project_name:
            argv += ["--project-name", target.compose_project_name]
        argv += ["config"]
        config_output = await self._run_docker_read(
            run,
            svc,
            tuple(argv),
            source_ref="docker:compose-config",
            description="effective compose config",
            now=now,
            evidence_refs=evidence_refs,
        )
        if config_output is None:
            return
        services_seen: list[str] = []
        try:
            parsed = json.loads(config_output)
            if isinstance(parsed, dict):
                services_seen = sorted(parsed.get("services", {}))
        except (json.JSONDecodeError, ValueError):
            # Older compose emits YAML; record the bounded output only.
            services_seen = []
        parts = [
            f"compose project-directory={target.compose_working_directory}",
            f"services={len(services_seen)}",
        ]
        if services_seen:
            parts.append(f"named={','.join(services_seen[:8])}")
        ref = self._record_discovery(
            run,
            svc,
            source_ref="docker:compose-config",
            discovery_kind="compose_config",
            description="; ".join(parts),
            now=now,
        )
        evidence_refs.append(self._evidence_ref(ref, ref.content_redacted[:800]))

    def _collect_container_candidates(
        self,
        run: AgentRun,
        svc: ServiceRegistration,
        ps_output: str,
        now: datetime,
        evidence_refs: list[EvidenceReference],
        candidates: list[DiscoveryCandidate],
    ) -> None:
        registered = set(svc.container_names)
        for line in ps_output.splitlines():
            name = line.strip()
            if not name or name in registered:
                continue
            description = (
                f"running container {name!r} is not registered for "
                f"service {svc.compose_service!r}; it will not be accessed"
            )
            ref = self._record_discovery(
                run,
                svc,
                source_ref=f"docker:ps:{name}",
                discovery_kind="unregistered_container",
                description=description,
                now=now,
            )
            summary = f"unregistered running container {name}"
            candidates.append(
                DiscoveryCandidate(
                    kind=DiscoveryCandidateKind.CONTAINER,
                    name=name,
                    service_name=svc.compose_service,
                    evidence_id=ref.evidence_ref_id,
                    summary=summary,
                )
            )
            evidence_refs.append(self._evidence_ref(ref, summary))

    # -- container discovery --------------------------------------------------

    async def _discover_container(
        self,
        run: AgentRun,
        svc: ServiceRegistration,
        container: str,
        now: datetime,
        evidence_refs: list[EvidenceReference],
        candidates: list[DiscoveryCandidate],
    ) -> None:
        self._validate_container(run, svc, container)
        inspect_output = await self._run_docker_read(
            run,
            svc,
            ("docker", "inspect", container),
            source_ref=f"docker:inspect:{container}",
            description=f"docker inspect {container}",
            now=now,
            evidence_refs=evidence_refs,
        )
        parsed = self._parse_inspect(inspect_output) if inspect_output else {}
        info = self._summarize_container_config(container, parsed)
        ref = self._record_discovery(
            run,
            svc,
            source_ref=f"container:{container}",
            discovery_kind="container_config",
            description=info,
            now=now,
        )
        evidence_refs.append(self._evidence_ref(ref, ref.content_redacted[:800]))
        self._collect_mount_candidates(
            run, svc, container, parsed.get("mounts", []), now, evidence_refs, candidates
        )
        await self._discover_container_paths(
            run, svc, container, now, evidence_refs, candidates
        )

    async def _discover_container_paths(
        self,
        run: AgentRun,
        svc: ServiceRegistration,
        container: str,
        now: datetime,
        evidence_refs: list[EvidenceReference],
        candidates: list[DiscoveryCandidate],
    ) -> None:
        """List a registered container's allowed path hints (file types)."""
        container = self._validate_container(run, svc, container)
        for hint in svc.container_path_hints[:_MAX_HOST_PATHS]:
            path = self._parse_path(hint)
            if not self._path_allowed(run, path, scope=LogScope.CONTAINER):
                continue
            try:
                entries = await self._gateway.list_dir(
                    project_id=run.scope.project_id,
                    target_id=run.scope.target_id,
                    service=svc.compose_service,
                    path=path,
                    scope={"kind": "container", "container": container},
                )
            except RemotePathError:
                continue
            rendered = "; ".join(
                f"{entry.path} type={self._file_type(entry.mode)} size={entry.size}"
                for entry in entries[:_MAX_LISTING_ENTRIES]
            )[:CONTENT_MAX_LENGTH]
            snapshot = self._evidence.record_file_snapshot(
                **self._evidence_kwargs(
                    run, svc.compose_service, f"container:{container}:{path}", now
                ),
                content=rendered or "(empty directory)",
                size_bytes=sum(entry.size for entry in entries),
            )
            summary = f"container {container} {path}: {len(entries)} entries"
            evidence_refs.append(self._evidence_ref(snapshot, summary))

    def _collect_mount_candidates(
        self,
        run: AgentRun,
        svc: ServiceRegistration,
        container: str,
        mounts: list[dict[str, Any]],
        now: datetime,
        evidence_refs: list[EvidenceReference],
        candidates: list[DiscoveryCandidate],
    ) -> None:
        for mount in mounts:
            if not isinstance(mount, dict):
                continue
            source = mount.get("Source")
            if not isinstance(source, str) or not source.startswith("/"):
                # Named volumes and anonymous mounts have no host path.
                continue
            candidate_path = self._parse_path(source)
            if self._path_allowed(run, candidate_path, scope=LogScope.HOST):
                continue
            description = (
                f"container {container} mounts host path {candidate_path}, which is "
                f"outside the registered allowed host paths for {svc.compose_service}"
            )
            ref = self._record_discovery(
                run,
                svc,
                source_ref=f"container:{container}:mount:{candidate_path}",
                discovery_kind="unregistered_mount_path",
                description=description,
                now=now,
            )
            summary = f"unregistered mount source {candidate_path}"
            candidates.append(
                DiscoveryCandidate(
                    kind=DiscoveryCandidateKind.HOST_PATH,
                    name=str(candidate_path),
                    service_name=svc.compose_service,
                    evidence_id=ref.evidence_ref_id,
                    summary=summary,
                )
            )
            evidence_refs.append(self._evidence_ref(ref, summary))

    # -- host path discovery --------------------------------------------------

    async def _discover_host_path(
        self,
        run: AgentRun,
        svc: ServiceRegistration,
        path: PurePosixPath,
        now: datetime,
        evidence_refs: list[EvidenceReference],
        candidates: list[DiscoveryCandidate],
    ) -> None:
        self._validate_path_in_scope(run, path, scope=LogScope.HOST)
        try:
            entries = await self._gateway.list_dir(
                project_id=run.scope.project_id,
                target_id=run.scope.target_id,
                service=svc.compose_service,
                path=path,
            )
        except RemotePathError:
            return
        bounded = entries[:_MAX_LISTING_ENTRIES]
        rendered = "; ".join(
            f"{entry.path} type={self._file_type(entry.mode)} size={entry.size}"
            for entry in bounded
        )[:CONTENT_MAX_LENGTH]
        snapshot = self._evidence.record_file_snapshot(
            **self._evidence_kwargs(run, svc.compose_service, str(path), now),
            content=rendered or "(empty directory)",
            size_bytes=sum(entry.size for entry in bounded),
        )
        evidence_refs.append(
            self._evidence_ref(
                snapshot, f"host path {path}: {len(entries)} entries"
            )
        )
        await self._collect_source_hashes(
            run,
            svc,
            bounded,
            now,
            evidence_refs,
            candidates,
        )

    async def _collect_source_hashes(
        self,
        run: AgentRun,
        svc: ServiceRegistration,
        entries: tuple[Any, ...],
        now: datetime,
        evidence_refs: list[EvidenceReference],
        candidates: list[DiscoveryCandidate],
    ) -> None:
        """Hash regular files under a listed host directory.

        Only files directly under the listed directory are read, bounded to
        ``_MAX_SOURCE_FILES`` and ``_MAX_SOURCE_FILE_BYTES``.  An entry that is
        somehow outside the run's allowed host paths is skipped, never read —
        path candidates are surfaced separately from ``docker inspect`` mounts.
        """
        hashes: list[str] = []
        reads = 0
        for entry in entries:
            if entry.is_symlink or not self._is_regular(entry.mode):
                continue
            if not self._path_allowed(run, entry.path, scope=LogScope.HOST):
                continue
            if reads >= _MAX_SOURCE_FILES:
                break
            reads += 1
            result = await self._gateway.read(
                project_id=run.scope.project_id,
                target_id=run.scope.target_id,
                service=svc.compose_service,
                path=entry.path,
                limit=min(entry.size, _MAX_SOURCE_FILE_BYTES),
            )
            text = result.content.decode("utf-8", errors="replace")
            ref = self._evidence.record_file_snapshot(
                **self._evidence_kwargs(
                    run, svc.compose_service, str(result.path), now
                ),
                content=text[:CONTENT_MAX_LENGTH],
                size_bytes=result.metadata.size,
            )
            hashes.append(f"{entry.path}={result.sha256[:16]}")
            evidence_refs.append(
                self._evidence_ref(
                    ref,
                    f"source {entry.path} sha256={result.sha256[:16]} "
                    f"size={result.metadata.size}",
                )
            )
        if not hashes:
            return
        description = "local source hashes: " + "; ".join(hashes[:_MAX_SOURCE_FILES])
        ref = self._record_discovery(
            run,
            svc,
            source_ref=str(entries[0].path.parent) if entries else "source",
            discovery_kind="source_hashes",
            description=description,
            now=now,
        )
        evidence_refs.append(self._evidence_ref(ref, ref.content_redacted[:800]))

    # -- small helpers --------------------------------------------------------

    @staticmethod
    def _outcome(
        service_name: str,
        evidence_refs: list[EvidenceReference],
        candidates: list[DiscoveryCandidate],
    ) -> DiscoveryOutcome:
        summary = (
            f"discovered {len(evidence_refs)} evidence item(s), "
            f"{len(candidates)} unregistered candidate(s) for {service_name}"
        )
        return DiscoveryOutcome(
            service_name=service_name,
            evidence=tuple(evidence_refs),
            candidates=tuple(candidates),
            summary=summary,
        )

    def _resolve_service(
        self, run: AgentRun, service_name: str
    ) -> ServiceRegistration:
        try:
            return self._gateway.resolve_service(
                run.scope.project_id, run.scope.target_id, service_name
            )
        except ValueError as exc:
            raise SourceDiscoveryError(str(exc))

    def _resolve_target(
        self, run: AgentRun, svc: ServiceRegistration
    ) -> TargetRegistration:
        project = self._projects.get(run.scope.project_id)
        for target in project.targets:
            if target.target_id == run.scope.target_id:
                return target
        raise SourceDiscoveryError(
            f"target {run.scope.target_id!r} is not registered for "
            f"project {run.scope.project_id!r}"
        )

    def _validate_container(
        self, run: AgentRun, svc: ServiceRegistration, container: str
    ) -> str:
        if container not in svc.container_names:
            raise SourceDiscoveryError(
                f"container {container!r} is not registered for service "
                f"{svc.compose_service!r}"
            )
        if run.scope.scope is LogScope.CONTAINER and (
            container != run.scope.container_name
            or svc.compose_service != run.scope.service_name
        ):
            raise SourceDiscoveryError(
                "container-scope run may only discover its own container"
            )
        return container

    @staticmethod
    def _assert_run_scope(run: AgentRun, scope: LogScope) -> None:
        if run.scope.scope is LogScope.CONTAINER and scope is LogScope.HOST:
            raise SourceDiscoveryError(
                "container-scope run may only discover inside its own container"
            )

    @staticmethod
    def _parse_path(value: str) -> PurePosixPath:
        path = PurePosixPath(value)
        if not path.is_absolute():
            raise SourceDiscoveryError(f"path must be absolute: {value}")
        if ".." in path.parts:
            raise SourceDiscoveryError(f"path must not contain '..': {value}")
        return path

    def _validate_path_in_scope(
        self, run: AgentRun, path: PurePosixPath, *, scope: LogScope
    ) -> PurePosixPath:
        allowed = (
            run.scope.allowed_host_paths
            if scope is LogScope.HOST
            else run.scope.allowed_container_paths
        )
        if allowed and not any(path.is_relative_to(root) for root in allowed):
            raise SourceDiscoveryError(
                f"path {path} is outside the run's allowed {scope.value} paths"
            )
        return path

    def _path_allowed(
        self, run: AgentRun, path: PurePosixPath, *, scope: LogScope
    ) -> bool:
        allowed = (
            run.scope.allowed_host_paths
            if scope is LogScope.HOST
            else run.scope.allowed_container_paths
        )
        return not allowed or any(path.is_relative_to(root) for root in allowed)

    async def _run_docker_read(
        self,
        run: AgentRun,
        svc: ServiceRegistration,
        argv: tuple[str, ...],
        *,
        source_ref: str,
        description: str,
        now: datetime,
        evidence_refs: list[EvidenceReference],
    ) -> str | None:
        """Run one fixed read-only docker argv on the host and record its output.

        Returns the stdout text, or ``None`` when the command failed (the failed
        run is still recorded as redacted command evidence).
        """
        target = self._resolve_target(run, svc)
        try:
            session = await self._sessions.connect(target)
            result = await session.transport.run_argv(argv, timeout=_DOCKER_TIMEOUT)
        except (RemoteTimeoutError, RemotePathError, OSError) as exc:
            ref = self._evidence.record_command_output(
                **self._evidence_kwargs(run, svc.compose_service, source_ref, now),
                command=" ".join(argv),
                output=f"discovery command failed: {exc}",
                exit_code=-1,
            )
            evidence_refs.append(
                self._evidence_ref(ref, f"{description} could not be confirmed")
            )
            return None
        stdout = result.stdout.decode("utf-8", errors="replace")
        ref = self._evidence.record_command_output(
            **self._evidence_kwargs(run, svc.compose_service, source_ref, now),
            command=" ".join(argv),
            output=stdout[:CONTENT_MAX_LENGTH],
            exit_code=result.exit_status,
        )
        evidence_refs.append(
            self._evidence_ref(ref, f"{description}: {len(stdout)} bytes")
        )
        if result.exit_status != 0:
            return None
        return stdout

    def _record_discovery(
        self,
        run: AgentRun,
        svc: ServiceRegistration,
        *,
        source_ref: str,
        discovery_kind: str,
        description: str,
        now: datetime,
    ) -> EvidenceRef:
        return self._evidence.record_registry_discovery(
            **self._evidence_kwargs(run, svc.compose_service, source_ref, now),
            discovery_kind=discovery_kind,
            description=description,
        )

    def _evidence_kwargs(
        self, run: AgentRun, service_name: str, source_ref: str, now: datetime
    ) -> dict[str, object]:
        return {
            "agent_run_id": run.agent_run_id,
            "incident_id": self._incident_id(run),
            "project_id": run.scope.project_id,
            "target_id": run.scope.target_id,
            "service_name": service_name,
            "source_ref": source_ref,
            "created_by": "agent",
            "now": now,
        }

    def _incident_id(self, run: AgentRun) -> str:
        try:
            investigation = self._investigations.get_investigation(run.investigation_id)
        except InvestigationNotFound as exc:
            raise SourceDiscoveryError(
                f"investigation {run.investigation_id!r} not found"
            ) from exc
        return investigation.incident_id

    @staticmethod
    def _evidence_ref(ref: EvidenceRef, summary: str) -> EvidenceReference:
        return EvidenceReference(
            evidence_id=ref.evidence_ref_id,
            operation_id=ref.source_ref or "",
            summary=summary,
        )

    @staticmethod
    def _parse_inspect(stdout: str) -> dict[str, Any]:
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return {}
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict):
            return {}
        config = data.get("Config")
        if not isinstance(config, dict):
            config = {}
        return {
            "image": data.get("Image") or "",
            "repo_digests": data.get("RepoDigests") or [],
            "labels": config.get("Labels") or {},
            "working_dir": config.get("WorkingDir") or "",
            "entrypoint": config.get("Entrypoint") or [],
            "cmd": config.get("Cmd") or [],
            "mounts": data.get("Mounts") or [],
        }

    @staticmethod
    def _summarize_container_config(container: str, parsed: dict[str, Any]) -> str:
        digests = parsed.get("repo_digests") or []
        digest = digests[0] if digests else parsed.get("image", "")
        start = " ".join(
            list(parsed.get("entrypoint") or []) + list(parsed.get("cmd") or [])
        )
        return (
            f"container={container} image={parsed.get('image', '')[:24]} "
            f"digest={digest[:32]} workdir={parsed.get('working_dir', '') or '(none)'} "
            f"start={start[:200] or '(none)'} labels={len(parsed.get('labels', {}))} "
            f"mounts={len(parsed.get('mounts', []))}"
        )

    @staticmethod
    def _file_type(mode: int) -> str:
        if mode & 0o170000 == 0o040000:
            return "directory"
        if mode & 0o170000 == 0o120000:
            return "symlink"
        return "file"

    @staticmethod
    def _is_regular(mode: int) -> bool:
        if mode & 0o170000:
            return mode & 0o170000 == 0o100000
        return True


__all__ = [
    "DiscoveryCandidate",
    "DiscoveryCandidateKind",
    "DiscoveryOutcome",
    "SourceDiscoveryError",
    "SourceDiscoveryService",
]
