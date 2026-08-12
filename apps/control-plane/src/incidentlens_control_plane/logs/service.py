"""On-demand log investigation pipeline.

Resolves the project/target/service registration, authorizes the requested
log source, queries the raw lines, runs the parse -> redact -> signal ->
correlation pipeline, and optionally persists redacted ``LogRecord`` rows.
Raw log text is only ever transient: only the redacted message reaches a
``LogRecord``.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from pathlib import PurePosixPath

from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.logs.correlation import extract_correlation_key
from incidentlens_control_plane.logs.parser import parse_log_line
from incidentlens_control_plane.logs.redaction import redact_message
from incidentlens_control_plane.logs.signals import detect_normal_signal
from incidentlens_control_plane.logs.sources import (
    DockerLogSource,
    FileLogSource,
    LogSourceUnavailable,
)
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.logs.types import (
    InvalidSubscription,
    LogQueryRequest,
    LogRecord,
    LogScope,
    LogSourceKind,
    LogSubscription,
    ProcessedLogLine,
    RawLogLine,
    ServiceNotFound,
    TargetNotFound,
    UnregisteredLogContainer,
)
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import (
    ProjectRecord,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.policy import (
    RemotePathDenied,
    RemotePathPolicy,
)
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.transport import RemoteTransport
from incidentlens_control_plane.remote_ops.types import HostScope


def _docker_dedupe_component(cursor: str) -> str:
    """Return the stable dedupe identity component for a docker cursor.

    Stream cursors are ``docker:time=<ts>:seq=<n>``; the per-stream ``seq``
    restarts on every reconnect, so dedupe identity must be built from the
    timestamp only.  That keeps replayed overlap lines (``docker --since`` is
    inclusive) producing the SAME dedupe_key as their originals.  Query cursors
    (``docker:<ref>:<offset>``) have no such replay hazard, so they fall back
    to the FULL cursor to keep offset-distinguished query records distinct.
    """
    prefix = "docker:time="
    if cursor.startswith(prefix):
        return cursor[len(prefix) :].split(":seq=", 1)[0]
    return cursor


class LogService:
    """Coordinate log source queries, redaction, and optional persistence."""

    def __init__(
        self,
        *,
        projects: ProjectRegistryStore,
        store: LogStore,
        sessions: SessionManager,
        evidence: EvidenceStore | None = None,
    ) -> None:
        self._projects = projects
        self._store = store
        self._sessions = sessions
        self._evidence = evidence

    async def query(
        self, request: LogQueryRequest, *, now: datetime
    ) -> tuple[LogRecord, ...]:
        if request.create_evidence:
            if not request.persist:
                raise ValueError("persist is required when create_evidence is True")
            if request.incident_id is None:
                raise ValueError("incident_id is required")
            if self._evidence is None:
                raise RuntimeError(
                    "evidence store is not configured for create_evidence"
                )

        project = self._projects.get(request.project_id)
        target = self._resolve_target(project, request.target_id)
        svc = self._resolve_service(project, request.service_name)

        raw_lines = await self._collect_raw_lines(request, target, svc)

        records = tuple(self._to_record(request, raw, now) for raw in raw_lines)
        if request.persist:
            self._store.append_batch(records)
            # Re-query by dedupe key so the returned records ARE the stored
            # rows.  append_batch dedupes on dedupe_key, so a re-poll must not
            # return freshly-generated log_ids that were never inserted.
            stored = self._store.records_by_dedupe_keys(
                tuple(record.dedupe_key for record in records)
            )
            if request.create_evidence:
                return tuple(
                    record.model_copy(
                        update={
                            "evidence_ref_id": self._evidence.create_from_log_record(
                                record,
                                incident_id=request.incident_id,
                                created_by="service",
                                now=now,
                            ).evidence_ref_id
                        }
                    )
                    for record in stored
                )
            return stored
        return records

    # --- internals ---

    async def _collect_raw_lines(
        self,
        request: LogQueryRequest,
        target: TargetRegistration,
        svc: ServiceRegistration,
    ) -> tuple[RawLogLine, ...]:
        if request.source_kind == LogSourceKind.DOCKER:
            if request.source_ref not in svc.container_names:
                raise UnregisteredLogContainer(
                    f"container {request.source_ref!r} is not a registered container"
                )
            session = await self._sessions.connect(target)
            source = DockerLogSource(lambda _target: session.transport)
            return await source.query(request, target)
        if request.source_kind == LogSourceKind.FILE:
            if request.scope == LogScope.CONTAINER:
                # Container file reads require a docker-exec file backend that
                # is not wired into LogService yet; the host SFTP transport
                # must not read a container path.
                raise LogSourceUnavailable("container file reads are not supported")
            session = await self._sessions.connect(target)
            path = await self._authorize_file_path(svc, request, session.transport)
            source = FileLogSource(self._sessions)
            return await source.query(request, target, path)
        raise ValueError(f"unsupported source kind: {request.source_kind}")

    async def _authorize_file_path(
        self,
        svc: ServiceRegistration,
        request: LogQueryRequest,
        transport: RemoteTransport,
    ) -> PurePosixPath:
        """Return the canonical path if the requested file is authorized.

        When ``allowed_log_paths`` is non-empty (the plan-mandated log
        allowlist) the path must stay under one of those roots, and the
        canonical (realpath-resolved) path must stay under that root and not be
        a symlink.  Otherwise the check falls back to ``RemotePathPolicy``,
        which authorizes against ``allowed_host_paths`` / ``allowed_container_paths``
        with the same canonical checks.
        """
        path = PurePosixPath(request.source_ref)
        if not path.is_absolute():
            raise RemotePathDenied(f"path is not absolute: {path}")
        if ".." in path.parts:
            raise RemotePathDenied(f"path contains '..': {path}")

        if svc.allowed_log_paths:
            matched_root = self._match_log_path_root(path, svc.allowed_log_paths)
            if matched_root is None:
                raise RemotePathDenied(
                    f"path {path} is outside allowed log paths {svc.allowed_log_paths}"
                )
            return await self._canonicalize_against_root(path, matched_root, transport)

        if request.scope == LogScope.CONTAINER:
            # Defensive: container file reads are rejected in _collect_raw_lines.
            raise RemotePathDenied("container file reads are not supported")
        policy = RemotePathPolicy(svc)
        return await policy.authorize(
            HostScope(), path, write=False, transport=transport
        )

    @staticmethod
    def _match_log_path_root(
        path: PurePosixPath, allowed_log_paths: tuple[str, ...]
    ) -> PurePosixPath | None:
        """Return the first allowed log-path root containing *path*, or None."""
        for root_str in allowed_log_paths:
            root = PurePosixPath(root_str)
            if path.is_relative_to(root):
                return root
        return None

    @staticmethod
    async def _canonicalize_against_root(
        path: PurePosixPath,
        root: PurePosixPath,
        transport: RemoteTransport,
    ) -> PurePosixPath:
        """Resolve *path* via the transport and deny escapes and symlinks."""
        canonical = await transport.realpath(path)
        if not canonical.is_relative_to(root):
            raise RemotePathDenied(
                f"resolved path {canonical} escapes allowed root {root}"
            )
        meta = await transport.lstat(canonical)
        if meta.is_symlink:
            raise RemotePathDenied(f"symlink detected at {canonical}")
        return canonical

    def _to_record(
        self, request: LogQueryRequest, raw: RawLogLine, now: datetime
    ) -> LogRecord:
        return self._record_from_raw(
            raw,
            now,
            project_id=request.project_id,
            target_id=request.target_id,
            service_name=request.service_name,
            source_kind=request.source_kind,
            scope=request.scope,
            source_ref=request.source_ref,
        )

    def _record_from_raw(
        self,
        raw: RawLogLine,
        now: datetime,
        *,
        project_id: str,
        target_id: str,
        service_name: str,
        source_kind: LogSourceKind,
        scope: LogScope,
        source_ref: str,
        subscription_id: str | None = None,
    ) -> LogRecord:
        parsed = parse_log_line(raw.text)
        if parsed.message_is_raw:
            # The line parsed as JSON but carried no message field, so
            # ``parsed.message`` is the raw JSON text.  Never persist that text
            # directly: redact the raw text explicitly so secrets inside the
            # structured line (e.g. ``{"password": "hunter2"}``) are replaced.
            redacted = redact_message(raw.text)
        else:
            redacted = redact_message(parsed.message)
        processed = ProcessedLogLine(
            raw=raw,
            parsed=parsed,
            message_redacted=redacted.message_redacted,
            redaction_summary=redacted.summary,
            normal_signal=detect_normal_signal(parsed),
            correlation_key=extract_correlation_key(parsed),
        )
        dedupe_source = "|".join(
            (
                project_id,
                target_id,
                service_name,
                source_kind.value,
                scope.value,
                source_ref,
                (
                    _docker_dedupe_component(raw.cursor)
                    if source_kind == LogSourceKind.DOCKER
                    else raw.cursor
                ),
                processed.message_redacted,
            )
        )
        return LogRecord(
            log_id=f"log-{uuid.uuid4().hex[:12]}",
            subscription_id=subscription_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            source_kind=source_kind,
            scope=scope,
            source_ref=source_ref,
            cursor=raw.cursor,
            dedupe_key=hashlib.sha256(dedupe_source.encode("utf-8")).hexdigest(),
            observed_at=now,
            event_time=parsed.event_time,
            severity=parsed.severity,
            message_redacted=processed.message_redacted,
            redaction_summary=processed.redaction_summary,
            normal_signal=processed.normal_signal,
            correlation_key=processed.correlation_key,
            evidence_ref_id=None,
            created_at=now,
        )

    def process_raw_lines(
        self,
        raw_lines: tuple[RawLogLine, ...],
        *,
        now: datetime,
        subscription: LogSubscription,
    ) -> tuple[LogRecord, ...]:
        """Run the parse -> redact -> signal -> correlation pipeline over raw lines.

        ``subscription`` supplies the record context (project/target/service/
        source kind and scope) that ``RawLogLine`` does not carry.  Raw log text
        is transient: only redacted ``LogRecord`` rows are returned.
        """
        return tuple(
            self._record_from_raw(
                raw,
                now,
                project_id=subscription.project_id,
                target_id=subscription.target_id,
                service_name=subscription.service_name,
                source_kind=subscription.source_kind,
                scope=subscription.scope,
                source_ref=subscription.source_ref,
                subscription_id=subscription.subscription_id,
            )
            for raw in raw_lines
        )

    async def validate_subscription_source(
        self,
        *,
        project_id: str,
        target_id: str,
        service_name: str,
        source_kind: LogSourceKind,
        scope: LogScope,
        source_ref: str,
    ) -> None:
        """Validate a subscription source against the registry, like ``query``.

        Resolves the project/target/service registration and checks the source
        reference WITHOUT contacting the remote target: a docker source must
        name a registered container, and a host file source must stay under the
        service's allowed log paths (or fall back to the allowed host paths).
        Raises ``ProjectNotFound``, ``TargetNotFound``, ``ServiceNotFound``,
        ``UnregisteredLogContainer``, ``RemotePathDenied`` or
        ``InvalidSubscription`` so a subscription can never be created for an
        unregistered source.
        """
        project = self._projects.get(project_id)
        self._resolve_target(project, target_id)
        svc = self._resolve_service(project, service_name)
        if source_kind == LogSourceKind.DOCKER:
            if source_ref not in svc.container_names:
                raise UnregisteredLogContainer(
                    f"container {source_ref!r} is not a registered container"
                )
            return
        if source_kind == LogSourceKind.FILE:
            if scope == LogScope.CONTAINER:
                raise InvalidSubscription("container file reads are not supported")
            path = PurePosixPath(source_ref)
            if not path.is_absolute():
                raise RemotePathDenied(f"path is not absolute: {path}")
            if ".." in path.parts:
                raise RemotePathDenied(f"path contains '..': {path}")
            if svc.allowed_log_paths:
                if self._match_log_path_root(path, svc.allowed_log_paths) is None:
                    raise RemotePathDenied(
                        f"path {path} is outside allowed log paths {svc.allowed_log_paths}"
                    )
            else:
                await RemotePathPolicy(svc).authorize(
                    HostScope(), path, write=False, transport=None
                )
            return
        raise InvalidSubscription(f"unsupported source kind: {source_kind}")

    @staticmethod
    def _resolve_target(
        project: ProjectRecord, target_id: str
    ) -> TargetRegistration:
        for target in project.targets:
            if target.target_id == target_id:
                return target
        raise TargetNotFound(
            f"target {target_id!r} is not registered for project {project.project_id!r}"
        )

    @staticmethod
    def _resolve_service(
        project: ProjectRecord, service_name: str
    ) -> ServiceRegistration:
        for svc in project.services:
            if svc.compose_service == service_name:
                return svc
        raise ServiceNotFound(
            f"service {service_name!r} not found in project {project.project_id!r}"
        )
