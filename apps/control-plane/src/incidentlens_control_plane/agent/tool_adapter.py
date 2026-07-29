"""LangChain tool adapter for read-only investigation tools.

Wraps ReadOnlyToolkit methods as LangChain StructuredTool instances
with evidence recording and deduplication.

Provides:
  - EvidenceRecorder: deduplicates evidence by normalized call key
  - AgentToolEnvelope: wraps tool result + evidence for LangChain responses
  - build_agent_tools: creates LangChain StructuredTool from ReadOnlyToolkit
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from incidentlens_contracts.models import Evidence, ToolResult
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from incidentlens_control_plane.agent.state import InvestigationAuditStore
from incidentlens_control_plane.tools.query import (
    GetRunbookArgs,
    GetServiceDependenciesArgs,
    GetSlowTracesArgs,
    GetTraceArgs,
    ListRecentDeploymentsArgs,
    QueryMetricsArgs,
    ReadOnlyToolkit,
    SearchLogsArgs,
)

# ---------------------------------------------------------------------------
# Stable hash for evidence deduplication
# ---------------------------------------------------------------------------


def stable_sha256(incident_id: str, tool_name: str, normalized_args: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 hex digest from normalized tool call inputs.

    The key is stable regardless of dict ordering because we sort keys
    before serializing to JSON.
    """
    canonical = json.dumps(
        {"incident_id": incident_id, "tool_name": tool_name, "args": normalized_args},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# AgentToolEnvelope
# ---------------------------------------------------------------------------


class AgentToolEnvelope(BaseModel):
    """Wraps a ReadOnlyTool result and its associated Evidence for LangChain.

    Placed in ToolMessage.artifact via response_format="content_and_artifact".
    """

    tool_result: ToolResult[Any]
    evidence: Evidence
    deduplicated: bool = False


# ---------------------------------------------------------------------------
# EvidenceRecorder
# ---------------------------------------------------------------------------


class EvidenceRecorder:
    """Records and deduplicates evidence from tool calls.

    Each unique (incident_id, tool_name, normalized_args) triple produces
    exactly one Evidence record. Duplicate calls return the existing evidence
    with deduplicated=True.
    """

    def __init__(self, audit_store: InvestigationAuditStore) -> None:
        self._audit_store = audit_store
        self._evidence_by_key: dict[str, Evidence] = {}

    def record(
        self,
        *,
        incident_id: str,
        tool_name: str,
        normalized_args: dict[str, Any],
        result: ToolResult[Any],
    ) -> tuple[Evidence, bool]:
        """Record evidence for a tool call, deduplicating by call key.

        Returns (evidence, is_duplicate).
        """
        call_key = stable_sha256(incident_id, tool_name, normalized_args)

        if call_key in self._evidence_by_key:
            return self._evidence_by_key[call_key], True

        evidence = Evidence(
            id=f"ev-{call_key[:16]}",
            source_tool=tool_name,
            tool_call_id=call_key,
            content={
                "incident_id": incident_id,
                "outcome": "success" if result.ok else "tool_error",
                "data": result.data,
                "error": result.error,
                "metadata": result.metadata,
            },
        )

        self._evidence_by_key[call_key] = evidence

        self._audit_store.record(
            incident_id,
            "evidence_recorded",
            {"call_key": call_key, "evidence": evidence.model_dump(mode="json")},
        )

        return evidence, False


# ---------------------------------------------------------------------------
# Tool mapping: tool name -> (args model, toolkit method name)
# ---------------------------------------------------------------------------

_TOOL_MAP: dict[str, tuple[type[BaseModel], str]] = {
    "query_metrics": (QueryMetricsArgs, "query_metrics"),
    "search_logs": (SearchLogsArgs, "search_logs"),
    "get_slow_traces": (GetSlowTracesArgs, "get_slow_traces"),
    "get_trace": (GetTraceArgs, "get_trace"),
    "get_service_dependencies": (GetServiceDependenciesArgs, "get_service_dependencies"),
    "list_recent_deployments": (ListRecentDeploymentsArgs, "list_recent_deployments"),
    "get_runbook": (GetRunbookArgs, "get_runbook"),
}

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "query_metrics": "Filter metric points by service, name, and time range.",
    "search_logs": "Search log rows by service and keyword.",
    "get_slow_traces": "Find traces with spans that exceed a duration threshold.",
    "get_trace": "Aggregate all spans for a given trace ID.",
    "get_service_dependencies": "Derive service dependency graph from span parent-child relationships.",
    "list_recent_deployments": "List recent deployments for a service.",
    "get_runbook": "Retrieve a runbook for a service.",
}


def build_agent_tools(
    toolkit: ReadOnlyToolkit,
    evidence_recorder: EvidenceRecorder,
    skill_runtime: Any | None = None,
) -> list[StructuredTool]:
    """Wrap ReadOnlyToolkit tools as LangChain StructuredTool instances.

    Each tool:
      - Validates Pydantic args before any repository call
      - Records evidence via EvidenceRecorder (deduplicates by call key)
      - Returns (summary_text, AgentToolEnvelope.model_dump()) for LangChain

    If skill_runtime is provided, a read_file tool is added for reading skills.
    """
    tools: list[StructuredTool] = []

    for tool_name, (args_model, method_name) in _TOOL_MAP.items():
        # Capture for closure -- default-arg binding avoids late-binding issues
        _tn = tool_name
        _am = args_model
        _mn = method_name
        _desc = _TOOL_DESCRIPTIONS[tool_name]

        async def _coroutine(
            _tool_name: str = _tn,
            _args_model: type[BaseModel] = _am,
            _method_name: str = _mn,
            **kwargs: Any,
        ) -> tuple[str, dict[str, Any]]:
            # Validate args through Pydantic (raises ValidationError if invalid)
            validated = _args_model.model_validate(kwargs)

            incident_id = getattr(validated, "incident_id", "")

            # Strip incident_id from kwargs (toolkit methods don't accept it)
            toolkit_kwargs = {
                k: v for k, v in validated.model_dump(exclude_unset=True).items()
                if k != "incident_id"
            }

            # Invoke the underlying ReadOnlyToolkit method
            toolkit_method = getattr(toolkit, _method_name)
            result: ToolResult[Any] = await toolkit_method(**toolkit_kwargs)

            # Record evidence (deduplicates by call key)
            evidence, is_duplicate = evidence_recorder.record(
                incident_id=incident_id,
                tool_name=_tool_name,
                normalized_args=toolkit_kwargs,
                result=result,
            )

            envelope = AgentToolEnvelope(
                tool_result=result,
                evidence=evidence,
                deduplicated=is_duplicate,
            )

            summary = f"{_tool_name}: {'ok' if result.ok else 'error'}"
            return summary, envelope.model_dump(mode="json")

        tool = StructuredTool.from_function(
            coroutine=_coroutine,
            name=tool_name,
            description=_desc,
            args_schema=args_model,
            response_format="content_and_artifact",
        )
        tools.append(tool)

    # Add read_file tool for skill reading (if skill_runtime provided)
    if skill_runtime is not None:

        class ReadFileArgs(BaseModel):
            path: str

        async def _read_file_coroutine(path: str = "", **kwargs: Any) -> tuple[str, dict[str, Any]]:
            actual_path = path or kwargs.get("path", "")
            result = await skill_runtime.read_file(actual_path)
            if result.ok:
                return result.content[:2000], {"ok": True, "content": result.content[:2000]}
            return f"error: {result.error}", {"ok": False, "error": result.error}

        read_file_tool = StructuredTool.from_function(
            coroutine=_read_file_coroutine,
            name="read_file",
            description="Read a file from the skills directory. Use paths like /skills/downstream-timeout/SKILL.md",
            args_schema=ReadFileArgs,
            response_format="content_and_artifact",
        )
        tools.append(read_file_tool)

    return tools
