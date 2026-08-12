"""Evidence-first executor that runs the agent-safe tool registry.

``ToolExecutor.execute`` turns a validated ``ToolRequest`` into a
``ToolOutcome``.  Every content-bearing result is first routed through
``EvidenceService`` so the model only ever receives evidence ids plus a bounded
summary built from *already redacted* content — raw file bytes, log lines and
command output never reach the model.  Before running anything it re-validates
the tool against its JSON schema and scope gate, resolves the registered
service/container, and bounds every requested path by the run's
``allowed_host_paths``/``allowed_container_paths`` and any child delegated
scope.  Shell/PTTY execution reuses the existing ``CommandPolicy`` +
``Gateway`` approval routing and the ``SessionManager`` SSH channel — there is
no second remote execution path.  A remote timeout or connection loss that
cannot confirm a result is recorded as ``UNCERTAIN`` (with UNCERTAIN_STATE
evidence) and is never auto-retried.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.changes.manager import (
    ChangeSetStatus,
    is_protected_path,
    protected_paths_intent,
)
from incidentlens_control_plane.evidence.service import (
    CONTENT_MAX_LENGTH,
    EvidenceService,
)
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.evidence.types import EvidenceRef
from incidentlens_control_plane.investigation.provider import (
    ToolRequest,
    ToolSchema,
    _scope_within,
    _validate_schema,
)
from incidentlens_control_plane.investigation.state_machine import ToolCallStatus
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.tools import (
    TOOL_CONTAINER_LIST,
    TOOL_CONTAINER_READ,
    TOOL_CONTAINER_SEARCH,
    TOOL_CONTAINER_STAT,
    TOOL_DEFINITIONS,
    TOOL_DELEGATE_CHILD,
    TOOL_DOCKER_ACTION,
    TOOL_EVIDENCE_LIST,
    TOOL_EVIDENCE_READ,
    TOOL_FILE_EDIT,
    TOOL_FILE_WRITE,
    TOOL_HOST_LIST,
    TOOL_HOST_READ,
    TOOL_HOST_SEARCH,
    TOOL_HOST_STAT,
    TOOL_LOG_CONTEXT,
    TOOL_LOG_QUERY,
    TOOL_LOG_SEARCH,
    TOOL_REGISTRY_INFO,
    TOOL_SERVICE_INFO,
    TOOL_SHELL_EXEC,
    TOOL_SOURCE_DISCOVER,
    ToolRegistry,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentScope,
    DelegatedTaskPackage,
    EvidenceReference,
)
from incidentlens_control_plane.logs.redaction import redact_message
from incidentlens_control_plane.logs.service import LogService
from incidentlens_control_plane.logs.store import LogSearchFilters, LogStore
from incidentlens_control_plane.logs.types import (
    LogQueryRequest,
    LogRecord,
    LogScope,
    LogSeverity,
    LogSourceKind,
)
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import (
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.gateway import (
    CommandForbidden,
    Gateway,
    RemoteToolGateway,
)
from incidentlens_control_plane.remote_ops.policy import CommandPolicy
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.shell import PersistentShell
from incidentlens_control_plane.remote_ops.transport import (
    RemoteConnectionError,
    RemoteTimeoutError,
)
from incidentlens_control_plane.remote_ops.types import (
    ChangeSetRequest,
    ContainerScope,
    DockerActionKind,
    DockerActionRequest,
    FileEditRequest,
    FileWriteRequest,
    HostScope,
    OperationRisk,
    ShellRequest,
    TextReplacement,
)

# Sentinel service name for run-scoped evidence that spans a whole host project
# (a host-scope run has no single service); the ownership check skips the
# service comparison for such runs.
_HOST_EVIDENCE_SERVICE = "host"

_MAX_FILE_READ_BYTES = 1_048_576


class ToolExecutionError(Exception):
    """A deterministic execution/validation failure safe to report to the model."""


class ToolUncertain(Exception):
    """A remote operation whose result cannot be confirmed (timeout/connection)."""


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool handler needs, scoped to one validated run."""

    run: AgentRun
    arguments: dict[str, Any]
    incident_id: str
    operation_id: str
    approval_id: str | None
    now: datetime


@dataclass(frozen=True)
class ToolResult:
    """Raw handler outcome before the executor wraps it into a ``ToolOutcome``."""

    status: ToolCallStatus = ToolCallStatus.SUCCEEDED
    evidence: tuple[EvidenceReference, ...] = ()
    summary: str = ""
    output_bytes: int = 0
    approval_id: str | None = None
    error_redacted: str | None = None


class ToolOutcome(BaseModel):
    """The single auditable result of one tool invocation, safe for the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: str = Field(min_length=1, max_length=120)
    tool_name: str = Field(min_length=1, max_length=120)
    status: ToolCallStatus
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=24)
    summary: str = Field(default="", max_length=4_000)
    output_bytes: int = Field(default=0, ge=0)
    approval_id: str | None = None
    error_redacted: str | None = Field(default=None, max_length=2_000)


class ToolExecutor:
    """Validates, executes and records every tool call for an agent run.

    The executor is deliberately dependency-injected with the existing services
    (``RemoteToolGateway``, ``LogService``/``LogStore``, ``SessionManager``,
    ``EvidenceService``, ``ApprovalService``, the investigation store) so there
    is exactly one SSH/Docker execution channel and one evidence pipeline.
    """

    def __init__(
        self,
        *,
        projects: ProjectRegistryStore,
        sessions: SessionManager,
        gateway: RemoteToolGateway,
        logs: LogService,
        log_store: LogStore,
        evidence: EvidenceService,
        evidence_store: EvidenceStore,
        investigations: InvestigationStore,
        approvals: ApprovalService,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._projects = projects
        self._sessions = sessions
        self._gateway = gateway
        self._logs = logs
        self._log_store = log_store
        self._evidence = evidence
        self._evidence_store = evidence_store
        self._investigations = investigations
        self._approvals = approvals
        self._registry = registry or default_tool_registry(self)

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    def tool_schemas(self, *, scope: LogScope) -> tuple[ToolSchema, ...]:
        """Return the provider-visible tool schemas for a run of *scope*."""
        return self._registry.tool_schemas(scope=scope)

    def requires_approval(self, tool_name: str) -> bool:
        return self._registry.requires_approval(tool_name)

    # -- public execution -----------------------------------------------------

    async def execute(
        self,
        request: ToolRequest,
        run: AgentRun,
        *,
        approval_id: str | None = None,
        now: datetime | None = None,
    ) -> ToolOutcome:
        """Validate *request* against the registry, run it, and record evidence."""
        definition = self._registry.get_definition(request.tool_name)
        if definition is None:
            return self._failed_outcome(
                request, ToolExecutionError(f"tool {request.tool_name!r} is not registered")
            )
        ok, message = _validate_schema(
            request.arguments, definition.parameters_json_schema, "arguments"
        )
        if not ok:
            return self._failed_outcome(
                request, ToolExecutionError(f"arguments invalid: {message}")
            )
        if (
            definition.allowed_scope is not None
            and definition.allowed_scope is not run.scope.scope
        ):
            return self._failed_outcome(
                request,
                ToolExecutionError(
                    f"tool {request.tool_name!r} requires "
                    f"{definition.allowed_scope.value} scope"
                ),
            )

        investigation = self._investigations.get_investigation(run.investigation_id)
        now = now or datetime.now(UTC)
        ctx = ToolContext(
            run=run,
            arguments=request.arguments,
            incident_id=investigation.incident_id,
            operation_id=request.tool_call_id,
            approval_id=approval_id,
            now=now,
        )
        handler = self._registry.handler_for(request.tool_name)
        try:
            result = await handler(ctx)  # type: ignore[misc]
        except ToolUncertain as exc:
            return self._uncertain_outcome(request, run, investigation.incident_id, exc)
        except ToolExecutionError as exc:
            return self._failed_outcome(request, exc)
        except CommandForbidden as exc:
            return self._failed_outcome(request, exc)
        except (asyncio.TimeoutError, RemoteTimeoutError, RemoteConnectionError) as exc:
            return self._uncertain_outcome(request, run, investigation.incident_id, exc)
        except Exception as exc:
            return self._failed_outcome(request, exc)
        return ToolOutcome(
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            status=result.status,
            evidence=result.evidence,
            summary=result.summary,
            output_bytes=result.output_bytes,
            approval_id=result.approval_id,
            error_redacted=result.error_redacted,
        )

    # -- log tools ------------------------------------------------------------

    async def _handle_log_query(self, ctx: ToolContext) -> ToolResult:
        args = ctx.arguments
        service_name = args["service_name"]
        source_kind = LogSourceKind(args["source_kind"])
        source_ref = args["source_ref"]
        svc = self._resolve_service(ctx, service_name)
        if source_kind is LogSourceKind.DOCKER:
            self._validate_container(ctx, svc, source_ref)
            scope = LogScope.CONTAINER
        else:
            self._validate_path_in_scope(ctx, self._parse_path(source_ref), scope=LogScope.HOST)
            scope = LogScope.HOST
        if ctx.run.scope.scope is LogScope.CONTAINER and (
            source_kind is not LogSourceKind.DOCKER
            or source_ref != ctx.run.scope.container_name
            or service_name != ctx.run.scope.service_name
        ):
            raise ToolExecutionError(
                "container-scope run may only query its own container logs"
            )
        records = await self._logs.query(
            LogQueryRequest(
                project_id=ctx.run.scope.project_id,
                target_id=ctx.run.scope.target_id,
                service_name=service_name,
                source_kind=source_kind,
                scope=scope,
                source_ref=source_ref,
                tail_lines=args.get("tail_lines", 100),
                persist=True,
                create_evidence=False,
            ),
            now=ctx.now,
        )
        if args.get("severity") is not None:
            records = tuple(
                record
                for record in records
                if record.severity is LogSeverity(args["severity"])
            )
        selected = self._fit_records_to_budget(ctx, records)
        return ToolResult(
            evidence=self._evidence_from_log_records(ctx, selected),
            summary=self._log_records_summary(selected, len(records)),
            output_bytes=sum(len(record.message_redacted) for record in selected),
        )

    async def _handle_log_search(self, ctx: ToolContext) -> ToolResult:
        args = ctx.arguments
        records = self._log_store.search(
            LogSearchFilters(
                project_id=ctx.run.scope.project_id,
                target_id=ctx.run.scope.target_id,
                service_name=args.get("service_name"),
                severity=(
                    LogSeverity(args["severity"]) if args.get("severity") else None
                ),
                text=args.get("text"),
                correlation_key=args.get("correlation_key"),
                normal_signal=args.get("normal_signal"),
                start_time=self._parse_dt(args.get("start_time")),
                end_time=self._parse_dt(args.get("end_time")),
            ),
            limit=min(args.get("limit", 50), 200),
        )
        selected = self._fit_records_to_budget(ctx, records)
        return ToolResult(
            evidence=self._evidence_from_log_records(ctx, selected),
            summary=self._log_records_summary(selected, len(records)),
            output_bytes=sum(len(record.message_redacted) for record in selected),
        )

    async def _handle_log_context(self, ctx: ToolContext) -> ToolResult:
        args = ctx.arguments
        correlation_key = args["correlation_key"]
        records = self._log_store.search(
            LogSearchFilters(
                project_id=ctx.run.scope.project_id,
                target_id=ctx.run.scope.target_id,
                service_name=args.get("service_name"),
                correlation_key=correlation_key,
                start_time=self._parse_dt(args.get("start_time")),
                end_time=self._parse_dt(args.get("end_time")),
            ),
            limit=min(args.get("limit", 50), 200),
        )
        selected = self._fit_records_to_budget(ctx, records)
        summary = (
            f"context for correlation_key={correlation_key}: "
            + self._log_records_summary(selected, len(records))
        )
        return ToolResult(
            evidence=self._evidence_from_log_records(ctx, selected),
            summary=summary,
            output_bytes=sum(len(record.message_redacted) for record in selected),
        )

    # -- evidence tools -------------------------------------------------------

    async def _handle_evidence_read(self, ctx: ToolContext) -> ToolResult:
        evidence_id = ctx.arguments["evidence_id"]
        owned = {ref.evidence_id for ref in ctx.run.evidence}
        if evidence_id not in owned:
            raise ToolExecutionError(
                f"evidence {evidence_id!r} was not collected by this run"
            )
        try:
            ref = self._evidence_store.get(evidence_id)
        except KeyError:
            raise ToolExecutionError(f"evidence {evidence_id!r} is not readable")
        metadata = ", ".join(f"{key}={value}" for key, value in ref.metadata.items())
        summary = (
            f"evidence {ref.evidence_ref_id} kind={ref.evidence_kind.value} "
            f"source={ref.source_ref or ''} metadata={{{metadata}}}: "
            f"{ref.content_redacted[:400]}"
        )
        return ToolResult(
            evidence=(
                EvidenceReference(
                    evidence_id=ref.evidence_ref_id,
                    operation_id=ctx.operation_id,
                    summary=summary,
                ),
            ),
            summary=summary,
            output_bytes=len(ref.content_redacted),
        )

    async def _handle_evidence_list(self, ctx: ToolContext) -> ToolResult:
        limit = min(ctx.arguments.get("limit", 50), 200)
        refs = self._evidence_store.query(
            agent_run_id=ctx.run.agent_run_id,
            incident_id=ctx.incident_id,
            limit=limit,
        )
        entries = [
            f"{ref.evidence_ref_id} kind={ref.evidence_kind.value} "
            f"source={ref.source_ref or ''}"
            for ref in refs
        ]
        joined = "; ".join(entries[:10])
        if len(entries) > 10:
            joined += f"; ... ({len(entries)} total)"
        summary = f"{len(refs)} evidence refs for run {ctx.run.agent_run_id}: {joined}"
        evidence = tuple(
            EvidenceReference(
                evidence_id=ref.evidence_ref_id,
                operation_id=ctx.operation_id,
                summary=entry,
            )
            for ref, entry in zip(refs, entries)
        )
        return ToolResult(evidence=evidence, summary=summary, output_bytes=len(summary))

    # -- registry / service tools ---------------------------------------------

    async def _handle_registry_info(self, ctx: ToolContext) -> ToolResult:
        project = self._projects.get(ctx.run.scope.project_id)
        lines = [f"project {project.project_id}"]
        for target in project.targets:
            lines.append(
                f"target {target.target_id} host={target.host} "
                f"compose_dir={target.compose_working_directory}"
            )
        for svc in project.services:
            lines.append(
                f"service {svc.compose_service} containers={sorted(svc.container_names)} "
                f"allowed_log_paths={sorted(svc.allowed_log_paths)}"
            )
        description = "; ".join(lines)
        ref = self._evidence.record_registry_discovery(
            agent_run_id=ctx.run.agent_run_id,
            incident_id=ctx.incident_id,
            project_id=ctx.run.scope.project_id,
            target_id=ctx.run.scope.target_id,
            service_name=self._evidence_service_name(ctx),
            source_ref="registry",
            discovery_kind="registry_info",
            description=description,
            created_by="agent",
            now=ctx.now,
        )
        summary = ref.content_redacted[:1_000]
        return ToolResult(
            evidence=(self._evidence_ref(ctx, ref, summary),),
            summary=summary,
            output_bytes=len(description),
        )

    async def _handle_service_info(self, ctx: ToolContext) -> ToolResult:
        service_name = ctx.arguments["service_name"]
        svc = self._resolve_service(ctx, service_name)
        description = (
            f"service {svc.compose_service}: containers={sorted(svc.container_names)} "
            f"allowed_host_paths={[str(p) for p in svc.allowed_host_paths]} "
            f"allowed_container_paths={[str(p) for p in svc.allowed_container_paths]} "
            f"allowed_log_paths={sorted(svc.allowed_log_paths)}"
        )
        ref = self._evidence.record_registry_discovery(
            agent_run_id=ctx.run.agent_run_id,
            incident_id=ctx.incident_id,
            project_id=ctx.run.scope.project_id,
            target_id=ctx.run.scope.target_id,
            service_name=service_name,
            source_ref=f"service:{service_name}",
            discovery_kind="service_info",
            description=description,
            created_by="agent",
            now=ctx.now,
        )
        summary = ref.content_redacted[:1_000]
        return ToolResult(
            evidence=(self._evidence_ref(ctx, ref, summary),),
            summary=summary,
            output_bytes=len(description),
        )

    # -- file tools (host + container) ----------------------------------------

    async def _handle_host_read(self, ctx: ToolContext) -> ToolResult:
        return await self._file_read(ctx, scope=LogScope.HOST)

    async def _handle_container_read(self, ctx: ToolContext) -> ToolResult:
        return await self._file_read(ctx, scope=LogScope.CONTAINER)

    async def _handle_host_list(self, ctx: ToolContext) -> ToolResult:
        return await self._file_list(ctx, scope=LogScope.HOST)

    async def _handle_container_list(self, ctx: ToolContext) -> ToolResult:
        return await self._file_list(ctx, scope=LogScope.CONTAINER)

    async def _handle_host_search(self, ctx: ToolContext) -> ToolResult:
        return await self._file_search(ctx, scope=LogScope.HOST)

    async def _handle_container_search(self, ctx: ToolContext) -> ToolResult:
        return await self._file_search(ctx, scope=LogScope.CONTAINER)

    async def _handle_host_stat(self, ctx: ToolContext) -> ToolResult:
        return await self._file_stat(ctx, scope=LogScope.HOST)

    async def _handle_container_stat(self, ctx: ToolContext) -> ToolResult:
        return await self._file_stat(ctx, scope=LogScope.CONTAINER)

    async def _file_read(self, ctx: ToolContext, *, scope: LogScope) -> ToolResult:
        args = ctx.arguments
        service_name = args["service_name"]
        path = self._parse_path(args["path"])
        svc = self._resolve_service(ctx, service_name)
        self._validate_path_in_scope(ctx, path, scope=scope)
        result = await self._gateway.read(
            **self._gateway_kwargs(
                ctx,
                service_name,
                path,
                scope,
                svc,
                extra={
                    "offset": args.get("offset", 0),
                    "limit": min(args.get("limit", _MAX_FILE_READ_BYTES), _MAX_FILE_READ_BYTES),
                },
            )
        )
        text = result.content.decode("utf-8", errors="replace")
        text = self._bound_content(ctx, text, CONTENT_MAX_LENGTH)
        ref = self._evidence.record_file_snapshot(
            **self._evidence_kwargs(ctx, service_name, str(result.path)),
            content=text,
            size_bytes=result.metadata.size,
        )
        preview = ref.content_redacted[:300]
        summary = (
            f"read {result.path} ({result.metadata.size} bytes, "
            f"truncated={result.truncated}); sha256={result.sha256[:16]}; {preview}"
        )
        return ToolResult(
            evidence=(self._evidence_ref(ctx, ref, summary),),
            summary=summary,
            output_bytes=len(text),
        )

    async def _file_list(self, ctx: ToolContext, *, scope: LogScope) -> ToolResult:
        args = ctx.arguments
        service_name = args["service_name"]
        path = self._parse_path(args["path"])
        svc = self._resolve_service(ctx, service_name)
        self._validate_path_in_scope(ctx, path, scope=scope)
        entries = await self._gateway.list_dir(
            **self._gateway_kwargs(ctx, service_name, path, scope, svc)
        )
        rendered = "; ".join(
            f"{entry.path} size={entry.size} mode={oct(entry.mode & 0o777)}"
            for entry in entries
        )
        rendered = self._bound_content(ctx, rendered, CONTENT_MAX_LENGTH)
        ref = self._evidence.record_file_snapshot(
            **self._evidence_kwargs(ctx, service_name, str(path)),
            content=rendered,
            size_bytes=sum(entry.size for entry in entries),
        )
        summary = f"{len(entries)} entries under {path}: {ref.content_redacted[:300]}"
        return ToolResult(
            evidence=(self._evidence_ref(ctx, ref, summary),),
            summary=summary,
            output_bytes=len(rendered),
        )

    async def _file_search(self, ctx: ToolContext, *, scope: LogScope) -> ToolResult:
        args = ctx.arguments
        service_name = args["service_name"]
        path = self._parse_path(args["path"])
        query = args["query"]
        svc = self._resolve_service(ctx, service_name)
        self._validate_path_in_scope(ctx, path, scope=scope)
        matches = await self._gateway.search(
            **self._gateway_kwargs(
                ctx,
                service_name,
                path,
                scope,
                svc,
                extra={"query": query},
            )
        )
        rendered = "; ".join(
            f"{match.path}:{match.line_number}: {match.text}" for match in matches
        )
        rendered = self._bound_content(ctx, rendered, CONTENT_MAX_LENGTH)
        ref = self._evidence.record_file_snapshot(
            **self._evidence_kwargs(ctx, service_name, str(path)),
            content=rendered,
            size_bytes=len(rendered),
        )
        summary = (
            f"{len(matches)} matches for {query!r} under {path}: "
            f"{ref.content_redacted[:300]}"
        )
        return ToolResult(
            evidence=(self._evidence_ref(ctx, ref, summary),),
            summary=summary,
            output_bytes=len(rendered),
        )

    async def _file_stat(self, ctx: ToolContext, *, scope: LogScope) -> ToolResult:
        args = ctx.arguments
        service_name = args["service_name"]
        path = self._parse_path(args["path"])
        svc = self._resolve_service(ctx, service_name)
        self._validate_path_in_scope(ctx, path, scope=scope)
        meta = await self._gateway.stat(
            **self._gateway_kwargs(ctx, service_name, path, scope, svc)
        )
        rendered = (
            f"path={meta.path} size={meta.size} mode={oct(meta.mode)} "
            f"uid={meta.uid} gid={meta.gid} mtime_ns={meta.modified_ns}"
        )
        ref = self._evidence.record_file_snapshot(
            **self._evidence_kwargs(ctx, service_name, str(meta.path)),
            content=rendered,
            size_bytes=meta.size,
        )
        return ToolResult(
            evidence=(self._evidence_ref(ctx, ref, rendered),),
            summary=rendered,
            output_bytes=len(rendered),
        )

    # -- source discovery / delegation ----------------------------------------

    async def _handle_source_discover(self, ctx: ToolContext) -> ToolResult:
        args = ctx.arguments
        service_name = args["service_name"]
        svc = self._resolve_service(ctx, service_name)
        candidates: list[str] = []
        container = args.get("container")
        path_str = args.get("path")
        if container is not None:
            container = self._validate_container(ctx, svc, container)
            candidates.append(f"container:{container}")
            candidates.extend(f"container-path:{hint}" for hint in svc.container_path_hints)
        elif path_str is not None:
            path = self._parse_path(path_str)
            self._validate_path_in_scope(ctx, path, scope=LogScope.HOST)
            entries = await self._gateway.list_dir(
                **self._gateway_kwargs(ctx, service_name, path, LogScope.HOST, svc)
            )
            candidates.extend(str(entry.path) for entry in entries[:100])
        else:
            candidates.extend(f"container:{name}" for name in svc.container_names)
            candidates.extend(f"log-path:{lp}" for lp in svc.allowed_log_paths)
            candidates.extend(f"container-path:{hint}" for hint in svc.container_path_hints)
        description = "; ".join(candidates) or f"no candidate log sources for {service_name}"
        ref = self._evidence.record_registry_discovery(
            agent_run_id=ctx.run.agent_run_id,
            incident_id=ctx.incident_id,
            project_id=ctx.run.scope.project_id,
            target_id=ctx.run.scope.target_id,
            service_name=service_name,
            source_ref=path_str or container or f"service:{service_name}",
            discovery_kind="log_source",
            description=description,
            created_by="agent",
            now=ctx.now,
        )
        summary = (
            f"discovered {len(candidates)} candidate log sources for "
            f"{service_name}: {ref.content_redacted[:800]}"
        )
        return ToolResult(
            evidence=(self._evidence_ref(ctx, ref, summary),),
            summary=summary,
            output_bytes=len(description),
        )

    async def _handle_delegate_child(self, ctx: ToolContext) -> ToolResult:
        args = ctx.arguments
        child_run_id = args["child_run_id"]
        if child_run_id == ctx.run.agent_run_id:
            raise ToolExecutionError("child_run_id must differ from the run id")
        child_scope = AgentScope(**args["scope"])
        allowed, reason = _scope_within(child_scope, ctx.run.scope)
        if not allowed:
            raise ToolExecutionError(f"child scope rejected: {reason}")
        seed = tuple(args.get("evidence_ids") or ())
        owned = {ref.evidence_id for ref in ctx.run.evidence}
        missing = set(seed) - owned
        if missing:
            raise ToolExecutionError(
                f"child delegation cites evidence not owned by this run: {sorted(missing)}"
            )
        budget = self._child_budget(ctx, args.get("budget") or {})
        package = DelegatedTaskPackage(
            child_run_id=child_run_id,
            parent_run_id=ctx.run.agent_run_id,
            investigation_id=ctx.run.investigation_id,
            task_prompt=args["task_prompt"],
            scope=child_scope,
            budget=budget,
            evidence_ids=seed,
        )
        self._investigations.create_delegated_task(package, now=ctx.now)
        description = (
            f"delegate child {child_run_id} scope={child_scope.scope.value} "
            f"project={child_scope.project_id} target={child_scope.target_id} "
            f"service={child_scope.service_name or 'host'} "
            f"prompt={args['task_prompt'][:200]}"
        )
        ref = self._evidence.record_registry_discovery(
            agent_run_id=ctx.run.agent_run_id,
            incident_id=ctx.incident_id,
            project_id=ctx.run.scope.project_id,
            target_id=ctx.run.scope.target_id,
            service_name=self._evidence_service_name(ctx),
            source_ref=f"child:{child_run_id}",
            discovery_kind="child_delegation",
            description=description,
            created_by="agent",
            now=ctx.now,
        )
        summary = (
            f"delegated child run {child_run_id} ({child_scope.scope.value} scope); "
            f"evidence {ref.evidence_ref_id}"
        )
        return ToolResult(
            evidence=(self._evidence_ref(ctx, ref, summary),),
            summary=summary,
            output_bytes=len(description),
        )

    # -- shell / changeset / docker (approval-aware) --------------------------

    async def _handle_shell_exec(self, ctx: ToolContext) -> ToolResult:
        args = ctx.arguments
        service_name = args["service_name"]
        command = args["command"]
        timeout = float(args.get("timeout_seconds", 30))
        svc = self._resolve_service(ctx, service_name)
        cwd_path = None
        if args.get("cwd") is not None:
            cwd_path = self._parse_path(args["cwd"])
            self._validate_path_in_scope(ctx, cwd_path, scope=LogScope.HOST)

        request = ShellRequest(
            operation_id=ctx.operation_id,
            incident_id=ctx.incident_id,
            project_id=ctx.run.scope.project_id,
            target_id=ctx.run.scope.target_id,
            service=service_name,
            scope=HostScope(),
            command=command,
            reason="agent investigation shell command",
        )
        decision = CommandPolicy().evaluate(request, svc)
        if decision.risk is OperationRisk.FORBIDDEN:
            raise ToolExecutionError(f"command is forbidden by policy: {decision.reason}")
        routed = await Gateway(self._approvals).shell(
            request.model_copy(update={"risk": decision.risk}),
            approval_id=ctx.approval_id,
        )
        if not routed.approved:
            return ToolResult(
                status=ToolCallStatus.WAITING_APPROVAL,
                approval_id=routed.approval_id,
                summary=f"command requires approval; approval_id={routed.approval_id}",
            )

        target = self._resolve_target(ctx)
        session = await self._sessions.connect(target)
        process = await session.transport.open_shell()
        shell = PersistentShell(process)
        framed = f"cd {cwd_path} && {command}" if cwd_path is not None else command
        try:
            shell_result = await shell.execute(framed, timeout=timeout)
        except asyncio.TimeoutError:
            raise ToolUncertain(f"shell command timed out after {timeout}s")
        finally:
            await shell.close()
        output = shell_result.stdout.decode("utf-8", errors="replace")
        bound = self._bound_content(ctx, output, CONTENT_MAX_LENGTH)
        ref = self._evidence.record_command_output(
            **self._evidence_kwargs(ctx, service_name, f"host:{ctx.run.scope.target_id}"),
            command=framed,
            output=bound,
            exit_code=shell_result.exit_status,
        )
        preview = ref.content_redacted[:300]
        summary = f"command exited {shell_result.exit_status}: {preview}"
        return ToolResult(
            evidence=(self._evidence_ref(ctx, ref, summary),),
            summary=summary,
            output_bytes=len(bound),
        )

    async def _handle_file_edit(self, ctx: ToolContext) -> ToolResult:
        return await self._file_mutation(ctx, write=False)

    async def _handle_file_write(self, ctx: ToolContext) -> ToolResult:
        return await self._file_mutation(ctx, write=True)

    async def _file_mutation(self, ctx: ToolContext, *, write: bool) -> ToolResult:
        args = ctx.arguments
        service_name = args["service_name"]
        path = self._parse_path(args["path"])
        scope = LogScope(args.get("scope", "host"))
        svc = self._resolve_service(ctx, service_name)
        self._validate_path_in_scope(ctx, path, scope=scope)
        remote_scope, container = self._scope_arg(ctx, svc, scope, args.get("container"))
        # The changeset id is derived from the tool-call id so the approval
        # intent requested up front names the same changeset that ``apply``
        # later consumes on an approved re-execution.
        changeset_id = f"chs-{ctx.operation_id[:110]}"
        operation_id = f"op-{ctx.operation_id[:100]}"
        if write:
            file_req: FileEditRequest | FileWriteRequest = FileWriteRequest(
                operation_id=operation_id,
                incident_id=ctx.incident_id,
                project_id=ctx.run.scope.project_id,
                target_id=ctx.run.scope.target_id,
                service=service_name,
                scope=remote_scope,
                path=path,
                content=args["content"].encode("utf-8"),
                mode=args.get("mode"),
                expected_sha256=args.get("expected_sha256"),
            )
            rollback_plan = "remove the file if the change fails"
        else:
            file_req = FileEditRequest(
                operation_id=operation_id,
                incident_id=ctx.incident_id,
                project_id=ctx.run.scope.project_id,
                target_id=ctx.run.scope.target_id,
                service=service_name,
                scope=remote_scope,
                path=path,
                expected_sha256=args["expected_sha256"],
                replacements=tuple(
                    TextReplacement(
                        old_text=item["old_text"],
                        new_text=item["new_text"],
                        expected_count=item.get("expected_count", 1),
                    )
                    for item in args["replacements"]
                ),
            )
            rollback_plan = "restore the verified timestamped backup"
        request = ChangeSetRequest(
            changeset_id=changeset_id,
            files=(file_req,),
            verification_plan="run syntax checks and compare service behavior",
            rollback_plan=rollback_plan,
        )
        pending = await self._approval_for_changeset(
            ctx, changeset_id, service_name, (path,), svc, ctx.approval_id
        )
        if pending is not None:
            return pending
        result = await self._gateway.apply_changeset(request, approval_id=ctx.approval_id)
        passed = result.status in (ChangeSetStatus.APPLIED, ChangeSetStatus.VERIFIED)
        detail = (
            f"changeset {result.changeset_id} status={result.status.value} "
            f"applied={result.applied_files} error={result.error or ''}"
        )
        if passed:
            ref = self._evidence.record_validation_result(
                **self._evidence_kwargs(ctx, service_name, str(path)),
                validator="changeset",
                passed=True,
                detail=detail,
            )
            summary = (
                f"changeset {result.changeset_id} {result.status.value}; "
                f"{len(result.applied_files)} file(s) affected"
            )
            return ToolResult(
                evidence=(self._evidence_ref(ctx, ref, summary),),
                summary=summary,
                output_bytes=len(detail),
            )
        # A deterministic apply failure is reported to the model as a redacted
        # error, exactly like any other failed tool.
        redacted = redact_message(detail, max_length=2_000).message_redacted
        return ToolResult(
            status=ToolCallStatus.FAILED,
            summary=f"tool failed: {redacted}",
            error_redacted=redacted,
        )

    async def _handle_docker_action(self, ctx: ToolContext) -> ToolResult:
        args = ctx.arguments
        service_name = args["service_name"]
        action = DockerActionKind(args["action"])
        container = args.get("container")
        reason = args.get("reason", "agent investigation action")
        svc = self._resolve_service(ctx, service_name)
        container_actions = {
            DockerActionKind.STOP,
            DockerActionKind.RESTART,
            DockerActionKind.KILL,
            DockerActionKind.REMOVE,
        }
        if ctx.run.scope.scope is LogScope.CONTAINER:
            if action not in container_actions or container is None:
                raise ToolExecutionError(
                    "container-scope run may only act on its own container"
                )
            self._validate_container(ctx, svc, container)
        elif container is not None:
            self._validate_container(ctx, svc, container)
        request = DockerActionRequest(
            operation_id=ctx.operation_id,
            incident_id=ctx.incident_id,
            project_id=ctx.run.scope.project_id,
            target_id=ctx.run.scope.target_id,
            service=service_name,
            scope=HostScope(),
            action=action,
            container=container,
            reason=reason,
        )
        result = await self._gateway.docker_action(request, approval_id=ctx.approval_id)
        if not result.approved:
            return ToolResult(
                status=ToolCallStatus.WAITING_APPROVAL,
                approval_id=result.approval_id,
                summary=(
                    f"docker {action.value} requires approval; "
                    f"approval_id={result.approval_id}"
                ),
            )
        target_ref = container or "compose"
        detail = f"docker action {action.value} on {target_ref} exit={result.exit_status}"
        ref = self._evidence.record_validation_result(
            **self._evidence_kwargs(ctx, service_name, target_ref),
            validator="docker_action",
            passed=True,
            detail=detail,
        )
        summary = f"docker {action.value} on {target_ref} exit={result.exit_status}"
        return ToolResult(
            evidence=(self._evidence_ref(ctx, ref, summary),),
            summary=summary,
            output_bytes=len(detail),
        )

    # -- validation helpers ---------------------------------------------------

    def _resolve_service(
        self, ctx: ToolContext, service_name: str
    ) -> ServiceRegistration:
        try:
            return self._gateway.resolve_service(
                ctx.run.scope.project_id, ctx.run.scope.target_id, service_name
            )
        except ValueError as exc:
            raise ToolExecutionError(str(exc))

    def _resolve_target(self, ctx: ToolContext) -> TargetRegistration:
        project = self._projects.get(ctx.run.scope.project_id)
        for target in project.targets:
            if target.target_id == ctx.run.scope.target_id:
                return target
        raise ToolExecutionError(
            f"target {ctx.run.scope.target_id!r} is not registered for "
            f"project {ctx.run.scope.project_id!r}"
        )

    def _validate_container(
        self, ctx: ToolContext, svc: ServiceRegistration, container: str
    ) -> str:
        if container not in svc.container_names:
            raise ToolExecutionError(
                f"container {container!r} is not registered for service "
                f"{svc.compose_service!r}"
            )
        if ctx.run.scope.scope is LogScope.CONTAINER and (
            container != ctx.run.scope.container_name
            or svc.compose_service != ctx.run.scope.service_name
        ):
            raise ToolExecutionError(
                "container-scope run may only operate on its own container"
            )
        return container

    def _validate_path_in_scope(
        self, ctx: ToolContext, path: PurePosixPath, *, scope: LogScope
    ) -> PurePosixPath:
        allowed = (
            ctx.run.scope.allowed_host_paths
            if scope is LogScope.HOST
            else ctx.run.scope.allowed_container_paths
        )
        if allowed and not any(path.is_relative_to(root) for root in allowed):
            raise ToolExecutionError(
                f"path {path} is outside the run's allowed {scope.value} paths"
            )
        return path

    @staticmethod
    def _parse_path(value: str) -> PurePosixPath:
        path = PurePosixPath(value)
        if not path.is_absolute():
            raise ToolExecutionError(f"path must be absolute: {value}")
        if ".." in path.parts:
            raise ToolExecutionError(f"path must not contain '..': {value}")
        return path

    def _scope_arg(
        self,
        ctx: ToolContext,
        svc: ServiceRegistration,
        scope: LogScope,
        container_arg: str | None,
    ) -> tuple[HostScope | ContainerScope, str | None]:
        if scope is LogScope.HOST:
            return HostScope(), None
        container = self._validate_container(ctx, svc, container_arg)
        return ContainerScope(container=container), container

    def _child_budget(self, ctx: ToolContext, budget_args: dict[str, Any]) -> AgentBudget:
        defaults = AgentBudget()
        kwargs = {
            name: budget_args.get(name, getattr(defaults, name))
            for name in AgentBudget.model_fields
        }
        budget = AgentBudget(**kwargs)
        base = ctx.run.budget
        for field_name, limit_name in (
            ("max_rounds", "max_rounds"),
            ("max_tool_calls", "max_tool_calls"),
            ("max_output_bytes_per_tool", "max_output_bytes_per_tool"),
            ("max_total_output_bytes", "max_total_output_bytes"),
        ):
            if getattr(budget, field_name) > getattr(base, limit_name):
                raise ToolExecutionError(
                    f"child {field_name} must not exceed the run budget "
                    f"({getattr(base, limit_name)})"
                )
        return budget

    async def _approval_for_changeset(
        self,
        ctx: ToolContext,
        changeset_id: str,
        service_name: str,
        paths: tuple[PurePosixPath, ...],
        svc: ServiceRegistration,
        approval_id: str | None,
    ) -> ToolResult | None:
        """Request an exact approval for protected paths, or return None to run."""
        protected = tuple(
            path for path in paths if is_protected_path(path, svc.protected_remote_paths)
        )
        if not protected or approval_id is not None:
            return None
        intent = protected_paths_intent(
            changeset_id=changeset_id,
            target_id=ctx.run.scope.target_id,
            service=service_name,
            paths=protected,
        )
        # Approvals are real-time artifacts: request them against the wall clock
        # so the TTL and the later approve/consume decision stay consistent,
        # independent of the investigation's simulated ``now``.
        record = await self._approvals.request(intent)
        return ToolResult(
            status=ToolCallStatus.WAITING_APPROVAL,
            approval_id=record.approval_id,
            summary=(
                f"changeset {changeset_id} touches protected path(s) "
                f"{[str(path) for path in protected]}; approval_id={record.approval_id}"
            ),
        )

    # -- shared argument/evidence builders ------------------------------------

    def _gateway_kwargs(
        self,
        ctx: ToolContext,
        service_name: str,
        path: PurePosixPath,
        scope: LogScope,
        svc: ServiceRegistration,
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "project_id": ctx.run.scope.project_id,
            "target_id": ctx.run.scope.target_id,
            "service": service_name,
            "path": path,
        }
        if scope is LogScope.CONTAINER:
            container = self._validate_container(ctx, svc, ctx.arguments.get("container"))
            kwargs["scope"] = {"kind": "container", "container": container}
        if extra:
            kwargs.update(extra)
        return kwargs

    def _evidence_kwargs(
        self,
        ctx: ToolContext,
        service_name: str,
        source_ref: str,
    ) -> dict[str, object]:
        return {
            "agent_run_id": ctx.run.agent_run_id,
            "incident_id": ctx.incident_id,
            "project_id": ctx.run.scope.project_id,
            "target_id": ctx.run.scope.target_id,
            "service_name": service_name,
            "source_ref": source_ref,
            "created_by": "agent",
            "now": ctx.now,
        }

    def _evidence_from_log_records(
        self, ctx: ToolContext, records: tuple[LogRecord, ...]
    ) -> tuple[EvidenceReference, ...]:
        refs: list[EvidenceReference] = []
        for record in records:
            ref = self._evidence.from_log_record(
                record,
                ctx.incident_id,
                "agent",
                ctx.now,
                agent_run_id=ctx.run.agent_run_id,
            )
            refs.append(
                EvidenceReference(
                    evidence_id=ref.evidence_ref_id,
                    operation_id=ctx.operation_id,
                    summary=self._log_record_preview(record),
                )
            )
        return tuple(refs)

    @staticmethod
    def _evidence_ref(ctx: ToolContext, ref: EvidenceRef, summary: str) -> EvidenceReference:
        return EvidenceReference(
            evidence_id=ref.evidence_ref_id, operation_id=ctx.operation_id, summary=summary
        )

    @staticmethod
    def _log_record_preview(record: LogRecord) -> str:
        return f"[{record.severity.value}] {record.source_ref}: {record.message_redacted[:200]}"

    def _log_records_summary(
        self, selected: tuple[LogRecord, ...], total: int
    ) -> str:
        previews = [self._log_record_preview(record) for record in selected[:5]]
        joined = "; ".join(previews)
        if len(selected) < total:
            joined += f"; (dropped {total - len(selected)} of {total} to stay within output budget)"
        return f"{len(selected)} log record(s): {joined}"

    def _fit_records_to_budget(
        self, ctx: ToolContext, records: tuple[LogRecord, ...]
    ) -> tuple[LogRecord, ...]:
        budget = ctx.run.budget.max_output_bytes_per_tool
        selected: list[LogRecord] = []
        total = 0
        for record in records:
            size = len(record.message_redacted)
            if selected and total + size > budget:
                break
            selected.append(record)
            total += size
        return tuple(selected)

    def _bound_content(self, ctx: ToolContext, text: str, cap: int) -> str:
        limit = min(ctx.run.budget.max_output_bytes_per_tool, cap)
        if len(text) > limit:
            return text[:limit]
        return text

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        if value is None:
            return None
        return datetime.fromisoformat(value)

    @staticmethod
    def _evidence_service_name(ctx: ToolContext) -> str:
        if ctx.run.scope.scope is LogScope.CONTAINER:
            return ctx.run.scope.service_name
        return ctx.run.scope.service_name or _HOST_EVIDENCE_SERVICE

    @staticmethod
    def _run_evidence_service_name(run: AgentRun) -> str:
        if run.scope.scope is LogScope.CONTAINER:
            return run.scope.service_name
        return run.scope.service_name or _HOST_EVIDENCE_SERVICE

    # -- outcome builders -----------------------------------------------------

    def _uncertain_outcome(
        self,
        request: ToolRequest,
        run: AgentRun,
        incident_id: str,
        reason: Exception,
    ) -> ToolOutcome:
        ref = self._evidence.record_uncertain_state(
            agent_run_id=run.agent_run_id,
            incident_id=incident_id,
            project_id=run.scope.project_id,
            target_id=run.scope.target_id,
            service_name=self._run_evidence_service_name(run),
            reason="unconfirmed_remote_state",
            description=str(reason),
            source_ref=None,
            created_by="agent",
            now=datetime.now(UTC),
        )
        summary = f"remote state could not be confirmed: {ref.content_redacted[:300]}"
        return ToolOutcome(
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            status=ToolCallStatus.UNCERTAIN,
            evidence=(
                EvidenceReference(
                    evidence_id=ref.evidence_ref_id,
                    operation_id=request.tool_call_id,
                    summary=summary,
                ),
            ),
            summary=summary,
            output_bytes=len(ref.content_redacted),
            error_redacted=summary,
        )

    def _failed_outcome(self, request: ToolRequest, error: Exception) -> ToolOutcome:
        redacted = redact_message(str(error), max_length=2_000).message_redacted
        return ToolOutcome(
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            status=ToolCallStatus.FAILED,
            summary=f"tool failed: {redacted}",
            error_redacted=redacted,
        )


def default_tool_registry(executor: ToolExecutor) -> ToolRegistry:
    """Bind every ``ToolDefinition`` to the executor's handler by name."""
    handlers: dict[str, Any] = {
        TOOL_LOG_QUERY: executor._handle_log_query,
        TOOL_LOG_SEARCH: executor._handle_log_search,
        TOOL_LOG_CONTEXT: executor._handle_log_context,
        TOOL_EVIDENCE_READ: executor._handle_evidence_read,
        TOOL_EVIDENCE_LIST: executor._handle_evidence_list,
        TOOL_REGISTRY_INFO: executor._handle_registry_info,
        TOOL_SERVICE_INFO: executor._handle_service_info,
        TOOL_HOST_READ: executor._handle_host_read,
        TOOL_HOST_LIST: executor._handle_host_list,
        TOOL_HOST_SEARCH: executor._handle_host_search,
        TOOL_HOST_STAT: executor._handle_host_stat,
        TOOL_CONTAINER_READ: executor._handle_container_read,
        TOOL_CONTAINER_LIST: executor._handle_container_list,
        TOOL_CONTAINER_SEARCH: executor._handle_container_search,
        TOOL_CONTAINER_STAT: executor._handle_container_stat,
        TOOL_SOURCE_DISCOVER: executor._handle_source_discover,
        TOOL_DELEGATE_CHILD: executor._handle_delegate_child,
        TOOL_SHELL_EXEC: executor._handle_shell_exec,
        TOOL_FILE_EDIT: executor._handle_file_edit,
        TOOL_FILE_WRITE: executor._handle_file_write,
        TOOL_DOCKER_ACTION: executor._handle_docker_action,
    }
    return ToolRegistry(TOOL_DEFINITIONS, handlers)


__all__ = [
    "ToolContext",
    "ToolExecutionError",
    "ToolExecutor",
    "ToolOutcome",
    "ToolResult",
    "ToolUncertain",
    "default_tool_registry",
]
