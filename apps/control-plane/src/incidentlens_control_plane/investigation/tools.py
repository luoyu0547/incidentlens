"""Agent-safe tool registry: names, JSON schemas, scope gating and approval flags.

The registry is the ONLY place the agent learns about the tools it may call.  A
``ToolDefinition`` carries the JSON argument schema, the allowed run scope and
whether the tool is statically approval-gated; ``ToolRegistry`` materializes
those into the ``ToolSchema`` objects a provider sees (through
``ProviderOutputValidator``) and binds each name to an async handler the
``ToolExecutor`` supplies.  Every definition stays typed: the arguments never
carry connection parameters, credentials or free-form commands that a raw
schema could smuggle past the validator.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from incidentlens_control_plane.investigation.provider import ToolSchema
from incidentlens_control_plane.logs.types import LogScope

# ---------------------------------------------------------------------------
# Tool names (single source of truth for the registry and the executor)
# ---------------------------------------------------------------------------

TOOL_LOG_QUERY = "log_query"
TOOL_LOG_SEARCH = "log_search"
TOOL_LOG_CONTEXT = "log_context"
TOOL_EVIDENCE_READ = "evidence_read"
TOOL_EVIDENCE_LIST = "evidence_list"
TOOL_REGISTRY_INFO = "registry_info"
TOOL_SERVICE_INFO = "service_info"
TOOL_HOST_READ = "host_read"
TOOL_HOST_LIST = "host_list"
TOOL_HOST_SEARCH = "host_search"
TOOL_HOST_STAT = "host_stat"
TOOL_CONTAINER_READ = "container_read"
TOOL_CONTAINER_LIST = "container_list"
TOOL_CONTAINER_SEARCH = "container_search"
TOOL_CONTAINER_STAT = "container_stat"
TOOL_SOURCE_DISCOVER = "source_discover"
TOOL_DELEGATE_CHILD = "delegate_child"
TOOL_SHELL_EXEC = "shell_exec"
TOOL_FILE_EDIT = "file_edit"
TOOL_FILE_WRITE = "file_write"
TOOL_DOCKER_ACTION = "docker_action"
TOOL_TODO_WRITE = "todo_write"
TOOL_COMPACT_CONTEXT = "compact_context"

# ---------------------------------------------------------------------------
# JSON-Schema fragments (the compact subset enforced by ``_validate_schema``)
# ---------------------------------------------------------------------------

_SEVERITY_ENUM = [
    "trace",
    "debug",
    "info",
    "notice",
    "warn",
    "error",
    "critical",
    "unknown",
]

_DOCKER_ACTION_ENUM = [
    "stop",
    "restart",
    "kill",
    "remove",
    "compose_stop",
    "compose_restart",
    "compose_down",
    "compose_up",
]

_SERVICE_NAME = {"type": "string", "minLength": 1, "maxLength": 120}
_CONTAINER = {"type": "string", "minLength": 1, "maxLength": 128}
_PATH = {"type": "string", "minLength": 1, "maxLength": 500}
_LIMIT_200 = {"type": "integer", "minimum": 1, "maximum": 200}
_ISO_TIME = {"type": "string", "minLength": 8, "maxLength": 40}


def _host_file_props(*, include_query: bool = False) -> dict[str, Any]:
    props: dict[str, Any] = {
        "service_name": _SERVICE_NAME,
        "path": _PATH,
    }
    if include_query:
        props["query"] = {"type": "string", "minLength": 1, "maxLength": 500}
    return props


def _container_file_props(*, include_query: bool = False) -> dict[str, Any]:
    props = _host_file_props(include_query=include_query)
    props["container"] = _CONTAINER
    return props


def _file_read_props(*, container: bool) -> dict[str, Any]:
    props = _container_file_props() if container else _host_file_props()
    props["offset"] = {"type": "integer", "minimum": 0, "maximum": 1_048_576}
    props["limit"] = {"type": "integer", "minimum": 1, "maximum": 1_048_576}
    return props


def _obj(props: dict[str, Any], *, required: Sequence[str] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _replacements_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": 200,
        "items": _obj(
            {
                "old_text": {"type": "string", "minLength": 1, "maxLength": 1_000_000},
                "new_text": {"type": "string", "maxLength": 1_000_000},
                "expected_count": {"type": "integer", "minimum": 1, "maximum": 1_000},
            },
            required=["old_text", "new_text"],
        ),
    }


def _child_scope_schema() -> dict[str, Any]:
    return _obj(
        {
            "project_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "target_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "scope": {"enum": ["host", "container"]},
            "service_name": {"type": "string", "minLength": 1, "maxLength": 120},
            "container_name": {"type": "string", "minLength": 1, "maxLength": 120},
            "allowed_host_paths": {
                "type": "array",
                "maxItems": 32,
                "items": {"type": "string", "minLength": 1},
            },
            "allowed_container_paths": {
                "type": "array",
                "maxItems": 32,
                "items": {"type": "string", "minLength": 1},
            },
        },
        required=["project_id", "target_id", "scope"],
    )


def _child_budget_schema() -> dict[str, Any]:
    return _obj(
        {
            "max_rounds": {"type": "integer", "minimum": 1, "maximum": 200},
            "max_tool_calls": {"type": "integer", "minimum": 1, "maximum": 500},
            "max_wall_clock_seconds": {"type": "integer", "minimum": 1, "maximum": 43_200},
            "max_output_bytes_per_tool": {"type": "integer", "minimum": 1, "maximum": 67_108_864},
            "max_total_output_bytes": {"type": "integer", "minimum": 1, "maximum": 134_217_728},
            "max_evidence": {"type": "integer", "minimum": 1, "maximum": 5_000},
            "max_no_new_evidence_rounds": {"type": "integer", "minimum": 1, "maximum": 20},
        }
    )


def _file_scope_props() -> dict[str, Any]:
    return {
        "scope": {"enum": ["host", "container"]},
        "container": _CONTAINER,
        "mode": {"type": "integer", "minimum": 0, "maximum": 0o7777},
    }


def _todo_item_schema() -> dict[str, Any]:
    """The argument shape of one work-plan item for ``todo_write``.

    ``todo_id``, ``content`` and ``status`` are required; ``evidence_ids`` and
    ``tool_call_ids`` are optional provenance links validated by the executor
    against the run's owned evidence.
    """
    return _obj(
        {
            "todo_id": {"type": "string", "minLength": 1, "maxLength": 120},
            "content": {"type": "string", "minLength": 1, "maxLength": 1_000},
            "status": {"enum": ["pending", "in_progress", "completed"]},
            "evidence_ids": {
                "type": "array",
                "maxItems": 32,
                "items": {"type": "string", "minLength": 1, "maxLength": 120},
            },
            "tool_call_ids": {
                "type": "array",
                "maxItems": 32,
                "items": {"type": "string", "minLength": 1, "maxLength": 120},
            },
        },
        required=["todo_id", "content", "status"],
    )


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolDefinition:
    """Static metadata for one agent tool; the executor binds a handler by name."""

    tool_name: str
    description: str
    parameters_json_schema: dict[str, Any]
    allowed_scope: LogScope | None = None
    requires_approval: bool = False
    output_cap_bytes: int = 512 * 1024
    concurrency_safe: bool = False


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        tool_name=TOOL_LOG_QUERY,
        concurrency_safe=True,
        description=(
            "Query the live tail of a log source (a registered docker container "
            "or an authorized host log file). Lines are redacted and persisted "
            "as LOG_RECORD evidence before you see them."
        ),
        parameters_json_schema=_obj(
            {
                "service_name": _SERVICE_NAME,
                "source_kind": {"enum": ["file", "docker"]},
                "source_ref": {"type": "string", "minLength": 1, "maxLength": 500},
                "tail_lines": {"type": "integer", "minimum": 1, "maximum": 1000},
                "severity": {"enum": _SEVERITY_ENUM},
            },
            required=["service_name", "source_kind", "source_ref"],
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_LOG_SEARCH,
        concurrency_safe=True,
        description=(
            "Search persisted redacted log records by severity, full-text, time "
            "bounds, correlation key or normal-signal label. Returns LOG_RECORD "
            "evidence for the matching rows."
        ),
        parameters_json_schema=_obj(
            {
                "service_name": _SERVICE_NAME,
                "severity": {"enum": _SEVERITY_ENUM},
                "text": {"type": "string", "minLength": 1, "maxLength": 200},
                "correlation_key": {"type": "string", "minLength": 1, "maxLength": 200},
                "normal_signal": {"type": "string", "minLength": 1, "maxLength": 120},
                "start_time": _ISO_TIME,
                "end_time": _ISO_TIME,
                "limit": _LIMIT_200,
            }
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_LOG_CONTEXT,
        concurrency_safe=True,
        description=(
            "Return the persisted log records sharing a correlation key (optionally "
            "bounded to a service and time window), so the correlated chain of "
            "events around one request is visible as evidence."
        ),
        parameters_json_schema=_obj(
            {
                "service_name": _SERVICE_NAME,
                "correlation_key": {"type": "string", "minLength": 1, "maxLength": 200},
                "start_time": _ISO_TIME,
                "end_time": _ISO_TIME,
                "limit": _LIMIT_200,
            },
            required=["correlation_key"],
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_EVIDENCE_READ,
        concurrency_safe=True,
        description=(
            "Read one evidence reference this run already collected, returning a "
            "bounded excerpt of its redacted content and metadata. Evidence not "
            "collected by this run is refused."
        ),
        parameters_json_schema=_obj(
            {"evidence_id": {"type": "string", "minLength": 1, "maxLength": 120}},
            required=["evidence_id"],
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_EVIDENCE_LIST,
        concurrency_safe=True,
        description="List the evidence references this run has collected, bounded.",
        parameters_json_schema=_obj({"limit": _LIMIT_200}),
    ),
    ToolDefinition(
        tool_name=TOOL_REGISTRY_INFO,
        concurrency_safe=True,
        description=(
            "Describe the registered project: targets, services, containers and "
            "allowed log paths the investigation may touch."
        ),
        parameters_json_schema=_obj({}),
    ),
    ToolDefinition(
        tool_name=TOOL_SERVICE_INFO,
        concurrency_safe=True,
        description=(
            "Describe one registered service: containers, allowed host/container "
            "paths and log paths."
        ),
        parameters_json_schema=_obj(
            {"service_name": _SERVICE_NAME}, required=["service_name"]
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_HOST_READ,
        concurrency_safe=True,
        description="Read a bounded slice of a file on the remote host.",
        parameters_json_schema=_obj(
            _file_read_props(container=False), required=["service_name", "path"]
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_HOST_LIST,
        concurrency_safe=True,
        description="List the entries of a directory on the remote host.",
        parameters_json_schema=_obj(_host_file_props(), required=["service_name", "path"]),
    ),
    ToolDefinition(
        tool_name=TOOL_HOST_SEARCH,
        concurrency_safe=True,
        description="Recursively search host files under a path for a query string.",
        parameters_json_schema=_obj(
            _host_file_props(include_query=True),
            required=["service_name", "path", "query"],
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_HOST_STAT,
        concurrency_safe=True,
        description="Return file metadata (size, mode, uid, gid, mtime) for a host path.",
        parameters_json_schema=_obj(_host_file_props(), required=["service_name", "path"]),
    ),
    ToolDefinition(
        tool_name=TOOL_CONTAINER_READ,
        concurrency_safe=True,
        description="Read a bounded slice of a file inside a registered container.",
        parameters_json_schema=_obj(
            _file_read_props(container=True),
            required=["service_name", "container", "path"],
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_CONTAINER_LIST,
        concurrency_safe=True,
        description="List the entries of a directory inside a registered container.",
        parameters_json_schema=_obj(
            _container_file_props(), required=["service_name", "container", "path"]
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_CONTAINER_SEARCH,
        concurrency_safe=True,
        description="Recursively search container files under a path for a query string.",
        parameters_json_schema=_obj(
            _container_file_props(include_query=True),
            required=["service_name", "container", "path", "query"],
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_CONTAINER_STAT,
        concurrency_safe=True,
        description="Return file metadata for a path inside a registered container.",
        parameters_json_schema=_obj(
            _container_file_props(), required=["service_name", "container", "path"]
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_SOURCE_DISCOVER,
        concurrency_safe=True,
        description=(
            "Discover candidate log sources for a service: registered containers, "
            "the allowed log paths, or the files under an authorized host path. "
            "Recorded as REGISTRY_DISCOVERY evidence to back a registry extension "
            "proposal."
        ),
        parameters_json_schema=_obj(
            {
                "service_name": _SERVICE_NAME,
                "path": _PATH,
                "container": _CONTAINER,
            },
            required=["service_name"],
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_DELEGATE_CHILD,
        description=(
            "Spawn an independent child run scoped as a legal narrowing of this "
            "run. The child scope, seed evidence and budget are validated before "
            "the delegated task is persisted."
        ),
        parameters_json_schema=_obj(
            {
                "child_run_id": {"type": "string", "minLength": 1, "maxLength": 120},
                "task_prompt": {"type": "string", "minLength": 1, "maxLength": 4_000},
                "scope": _child_scope_schema(),
                "budget": _child_budget_schema(),
                "evidence_ids": {
                    "type": "array",
                    "maxItems": 24,
                    "items": {"type": "string", "minLength": 1, "maxLength": 120},
                },
            },
            required=["child_run_id", "task_prompt", "scope"],
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_SHELL_EXEC,
        description=(
            "Run one shell command on the remote host through the persistent shell. "
            "The command is classified by policy: read-only commands run "
            "automatically, mutating commands require an exact approval, forbidden "
            "commands never run. Output is recorded as redacted COMMAND_OUTPUT "
            "evidence. Prefer typed file/log tools; shell is for what they cannot "
            "express."
        ),
        parameters_json_schema=_obj(
            {
                "service_name": _SERVICE_NAME,
                "command": {"type": "string", "minLength": 1, "maxLength": 8_000},
                "cwd": _PATH,
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 120},
            },
            required=["service_name", "command"],
        ),
        allowed_scope=LogScope.HOST,
    ),
    ToolDefinition(
        tool_name=TOOL_FILE_EDIT,
        description=(
            "Apply exact text replacements to a remote file inside a backup-and-"
            "verify changeset. Protected paths require an exact approval; the "
            "approval intent names the canonical change."
        ),
        parameters_json_schema=_obj(
            {
                "service_name": _SERVICE_NAME,
                "path": _PATH,
                "expected_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                "replacements": _replacements_schema(),
                "scope": {"enum": ["host", "container"]},
                "container": _CONTAINER,
            },
            required=["service_name", "path", "expected_sha256", "replacements"],
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_FILE_WRITE,
        description=(
            "Write a new remote file (or overwrite with a matching expected hash) "
            "inside a backup-and-verify changeset. Protected paths require an "
            "exact approval."
        ),
        parameters_json_schema=_obj(
            {
                "service_name": _SERVICE_NAME,
                "path": _PATH,
                "content": {"type": "string", "minLength": 1, "maxLength": 10_485_760},
                "mode": {"type": "integer", "minimum": 0, "maximum": 0o7777},
                "expected_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                "scope": {"enum": ["host", "container"]},
                "container": _CONTAINER,
            },
            required=["service_name", "path", "content"],
        ),
    ),
    ToolDefinition(
        tool_name=TOOL_DOCKER_ACTION,
        description=(
            "Run a fixed, typed docker/compose action (stop/restart/kill/remove, "
            "compose up/down) against a registered container or the compose stack. "
            "Always requires an exact approval and is never given an arbitrary "
            "command."
        ),
        parameters_json_schema=_obj(
            {
                "service_name": _SERVICE_NAME,
                "action": {"enum": _DOCKER_ACTION_ENUM},
                "container": _CONTAINER,
                "reason": {"type": "string", "minLength": 1, "maxLength": 1_000},
            },
            required=["service_name", "action"],
        ),
        requires_approval=True,
    ),
    ToolDefinition(
        tool_name=TOOL_TODO_WRITE,
        description=(
            "Replace this run's work plan with the given items. Each item carries "
            "a unique todo_id, the content, a status, and optional provenance "
            "links (evidence/tool-call ids this run owns). At most one item may "
            "be in_progress. The plan is local to the run and never touches "
            "remote state, so complex investigations should keep it current "
            "before running unrelated tools."
        ),
        parameters_json_schema=_obj(
            {
                "todos": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "items": _todo_item_schema(),
                }
            },
            required=["todos"],
        ),
        concurrency_safe=True,
    ),
    ToolDefinition(
        tool_name=TOOL_COMPACT_CONTEXT,
        description=(
            "Ask the harness to compact this run's active context into a bounded "
            "session memory revision. This is a local control request: it never "
            "touches remote state and requires no approval. It is only "
            "meaningful when the run is starting to exhaust its context window."
        ),
        parameters_json_schema={"type": "object", "additionalProperties": False},
    ),
)


class ToolRegistry:
    """Binds static ``ToolDefinition``s to executor handlers, name-addressable."""

    def __init__(
        self,
        definitions: Sequence[ToolDefinition],
        handlers: Mapping[str, Callable[..., Awaitable[object]]],
    ) -> None:
        self._definitions = {definition.tool_name: definition for definition in definitions}
        self._handlers: dict[str, Callable[..., Awaitable[object]]] = dict(handlers)
        unknown = set(self._handlers) - set(self._definitions)
        if unknown:
            raise ValueError(f"handlers for unknown tools: {sorted(unknown)}")
        missing = set(self._definitions) - set(self._handlers)
        if missing:
            raise ValueError(f"definitions missing handlers: {sorted(missing)}")

    def get_definition(self, tool_name: str) -> ToolDefinition | None:
        return self._definitions.get(tool_name)

    def handler_for(self, tool_name: str) -> Callable[..., Awaitable[object]] | None:
        return self._handlers.get(tool_name)

    def tool_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def requires_approval(self, tool_name: str) -> bool:
        definition = self._definitions.get(tool_name)
        return bool(definition is not None and definition.requires_approval)

    def tool_schemas(self, *, scope: LogScope) -> tuple[ToolSchema, ...]:
        """Return the ``ToolSchema``s a run of *scope* may call.

        Tools pinned to the other scope are excluded, so a container run never
        sees ``shell_exec`` and a host run never sees a container-only tool.
        """
        schemas: list[ToolSchema] = []
        for name in sorted(self._definitions):
            definition = self._definitions[name]
            if definition.allowed_scope is not None and definition.allowed_scope is not scope:
                continue
            schemas.append(
                ToolSchema(
                    tool_name=definition.tool_name,
                    description=definition.description,
                    parameters_json_schema=definition.parameters_json_schema,
                    allowed_scope=definition.allowed_scope,
                    output_cap_bytes=definition.output_cap_bytes,
                    requires_approval=definition.requires_approval,
                    concurrency_safe=definition.concurrency_safe,
                )
            )
        return tuple(schemas)


__all__ = [
    "TOOL_COMPACT_CONTEXT",
    "TOOL_CONTAINER_LIST",
    "TOOL_CONTAINER_READ",
    "TOOL_CONTAINER_SEARCH",
    "TOOL_CONTAINER_STAT",
    "TOOL_DELEGATE_CHILD",
    "TOOL_DEFINITIONS",
    "TOOL_DOCKER_ACTION",
    "TOOL_EVIDENCE_LIST",
    "TOOL_EVIDENCE_READ",
    "TOOL_FILE_EDIT",
    "TOOL_FILE_WRITE",
    "TOOL_HOST_LIST",
    "TOOL_HOST_READ",
    "TOOL_HOST_SEARCH",
    "TOOL_HOST_STAT",
    "TOOL_LOG_CONTEXT",
    "TOOL_LOG_QUERY",
    "TOOL_LOG_SEARCH",
    "TOOL_REGISTRY_INFO",
    "TOOL_SERVICE_INFO",
    "TOOL_SHELL_EXEC",
    "TOOL_SOURCE_DISCOVER",
    "TOOL_TODO_WRITE",
    "ToolDefinition",
    "ToolRegistry",
]
