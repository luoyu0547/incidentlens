"""Tests for tool_adapter — LangChain tool boundary and evidence recording.

TDD RED phase: these tests define the desired interface for:
  - EvidenceRecorder: deduplicates evidence by normalized call key
  - build_agent_tools: wraps ReadOnlyToolkit tools as LangChain StructuredTool
  - AgentToolEnvelope: wraps tool result + evidence for LangChain responses
"""

import pytest
from pydantic import ValidationError

from incidentlens_control_plane.agent.tool_adapter import (
    EvidenceRecorder,
    build_agent_tools,
)


def _tool_call(tool_name: str, args: dict, call_id: str = "call-1") -> dict:
    """Build a LangChain ToolCall-format dict for ainvoke."""
    return {
        "name": tool_name,
        "args": args,
        "id": call_id,
        "type": "tool_call",
    }


async def test_agent_tool_executes_existing_tool_and_records_owned_evidence(
    toolkit,
    investigation_audit_store,
) -> None:
    """An agent tool invocation should delegate to the underlying ReadOnlyToolkit,
    record evidence, and return an envelope with both tool_result and evidence."""
    recorder = EvidenceRecorder(investigation_audit_store)
    tools = {tool.name: tool for tool in build_agent_tools(toolkit, recorder)}

    response = await tools["search_logs"].ainvoke(
        _tool_call(
            "search_logs",
            {
                "incident_id": "inc-1",
                "service": "order-service",
                "keyword": "timeout",
                "limit": 10,
            },
        )
    )
    result = response.artifact

    assert result["tool_result"]["ok"] is True
    assert result["evidence"]["id"]
    assert result["evidence"]["content"]["incident_id"] == "inc-1"
    assert result["evidence"]["source_tool"] == "search_logs"


async def test_duplicate_normalized_call_reuses_evidence_id(
    toolkit, investigation_audit_store
) -> None:
    """Two calls with identical normalized args should produce the same evidence ID,
    and the second call should be marked as deduplicated."""
    recorder = EvidenceRecorder(investigation_audit_store)
    tool = {
        tool.name: tool for tool in build_agent_tools(toolkit, recorder)
    }["search_logs"]
    args = {
        "incident_id": "inc-1",
        "service": "order-service",
        "keyword": "timeout",
        "limit": 10,
    }
    first_response = await tool.ainvoke(_tool_call("search_logs", args, "call-1"))
    second_response = await tool.ainvoke(_tool_call("search_logs", args, "call-2"))
    first = first_response.artifact
    second = second_response.artifact

    assert second["deduplicated"] is True
    assert second["evidence"]["id"] == first["evidence"]["id"]


async def test_invalid_tool_args_are_rejected_before_repository(
    toolkit,
    investigation_audit_store,
) -> None:
    """Invalid Pydantic input (e.g. wrong types) should raise ValidationError
    before any repository call is made."""
    recorder = EvidenceRecorder(investigation_audit_store)
    tool = {
        tool.name: tool for tool in build_agent_tools(toolkit, recorder)
    }["query_metrics"]
    with pytest.raises(ValidationError):
        await tool.ainvoke(
            _tool_call(
                "query_metrics",
                {
                    "incident_id": "inc-1",
                    "service": "order-service",
                    "limit": "not_a_number",
                },
                "call-invalid",
            )
        )
