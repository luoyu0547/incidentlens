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

from incidentlens_control_plane.logs.correlation import extract_correlation_key
from incidentlens_control_plane.logs.parser import parse_log_line
from incidentlens_control_plane.logs.redaction import redact_message
from incidentlens_control_plane.logs.signals import detect_normal_signal
from incidentlens_control_plane.logs.sources import DockerLogSource, FileLogSource
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.logs.types import (
    LogQueryRequest,
    LogRecord,
    LogScope,
    LogSourceKind,
    ProcessedLogLine,
    RawLogLine,
)
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import (
    ProjectRecord,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.policy import RemotePathDenied
from incidentlens_control_plane.remote_ops.sessions import SessionManager


class LogService:
    """Coordinate log source queries, redaction, and optional persistence."""

    def __init__(
        self,
        *,
        projects: ProjectRegistryStore,
        store: LogStore,
        sessions: SessionManager,
    ) -> None:
        self._projects = projects
        self._store = store
        self._sessions = sessions

    async def query(
        self, request: LogQueryRequest, *, now: datetime
    ) -> tuple[LogRecord, ...]:
        project = self._projects.get(request.project_id)
        target = self._resolve_target(project, request.target_id)
        svc = self._resolve_service(project, request.service_name)

        raw_lines = await self._collect_raw_lines(request, target, svc)

        records = tuple(self._to_record(request, raw, now) for raw in raw_lines)
        if request.persist:
            self._store.append_batch(records)
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
                raise ValueError(
                    f"container {request.source_ref!r} is not a registered container"
                )
            session = await self._sessions.connect(target)
            source = DockerLogSource(lambda _target: session.transport)
            return await source.query(request, target)
        if request.source_kind == LogSourceKind.FILE:
            path = self._authorize_file_path(svc, request)
            source = FileLogSource(self._sessions)
            return await source.query(request, target, path)
        raise ValueError(f"unsupported source kind: {request.source_kind}")

    def _authorize_file_path(
        self, svc: ServiceRegistration, request: LogQueryRequest
    ) -> PurePosixPath:
        """Return the requested file path if it stays within an allowed root."""
        path = PurePosixPath(request.source_ref)
        if not path.is_absolute():
            raise RemotePathDenied(f"path is not absolute: {path}")
        if ".." in path.parts:
            raise RemotePathDenied(f"path contains '..': {path}")

        if svc.allowed_log_paths:
            roots = tuple(PurePosixPath(root) for root in svc.allowed_log_paths)
        elif request.scope == LogScope.HOST:
            roots = svc.allowed_host_paths
        else:
            roots = svc.allowed_container_paths

        if not any(path.is_relative_to(root) for root in roots):
            raise RemotePathDenied(f"path {path} is outside allowed roots {roots}")
        return path

    def _to_record(
        self, request: LogQueryRequest, raw: RawLogLine, now: datetime
    ) -> LogRecord:
        parsed = parse_log_line(raw.text)
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
                request.project_id,
                request.target_id,
                request.service_name,
                request.source_kind.value,
                request.scope.value,
                request.source_ref,
                raw.cursor,
                processed.message_redacted,
            )
        )
        return LogRecord(
            log_id=f"log-{uuid.uuid4().hex[:12]}",
            subscription_id=None,
            project_id=request.project_id,
            target_id=request.target_id,
            service_name=request.service_name,
            source_kind=request.source_kind,
            scope=request.scope,
            source_ref=request.source_ref,
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

    @staticmethod
    def _resolve_target(
        project: ProjectRecord, target_id: str
    ) -> TargetRegistration:
        for target in project.targets:
            if target.target_id == target_id:
                return target
        raise ValueError(
            f"target {target_id!r} is not registered for project {project.project_id!r}"
        )

    @staticmethod
    def _resolve_service(
        project: ProjectRecord, service_name: str
    ) -> ServiceRegistration:
        for svc in project.services:
            if svc.compose_service == service_name:
                return svc
        raise ValueError(
            f"service {service_name!r} not found in project {project.project_id!r}"
        )
