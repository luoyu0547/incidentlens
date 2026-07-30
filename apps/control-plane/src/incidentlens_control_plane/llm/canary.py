"""Live Canary for verifying model tool-calling capability."""
import secrets
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .registry import ModelIdentity, ModelRegistry


class CanaryArgs(BaseModel):
    """Input schema for the canary tool."""
    nonce: str = Field(description="A random nonce to echo back for audit verification")


class ProposalCanaryArgs(BaseModel):
    """Input schema for the proposal canary tool."""
    root_service: str = Field(description="The root service")
    cause_code: str = Field(description="The cause code")
    evidence_ids: list[str] = Field(description="Evidence IDs")
    confidence: float = Field(ge=0, le=1, description="Confidence score")
    next_action: str = Field(description="next_action")


@dataclass(frozen=True)
class CanaryResult:
    """Result of a successful canary test."""
    nonce: str
    tool_name: str
    audit_nonce: str
    identity: ModelIdentity
    fallback_used: bool


@dataclass(frozen=True)
class SchemaCanaryResult:
    """Result of a schema-based canary test."""
    normal_tool_call_passed: bool
    proposal_tool_call_passed: bool
    fallback_used: bool
    identity: ModelIdentity = field(default_factory=lambda: ModelIdentity("", "", ""))


async def run_model_canary(
    registry: ModelRegistry,
    profile_name: str,
    nonce: str | None = None,
) -> CanaryResult:
    """Run a live canary test to verify a model can perform tool calls.

    This function sends a minimal prompt asking the model to call the
    `incidentlens_canary` tool with a random nonce. If the model successfully
    invokes the tool with the correct nonce, the canary passes.

    Args:
        registry: The model registry to get the chat model from.
        profile_name: The profile name to test.
        nonce: Optional nonce. If not provided, a random one is generated.

    Returns:
        A CanaryResult with the nonce, tool name, audit nonce, and identity.

    Raises:
        AssertionError: If the model fails to call the tool or returns the wrong nonce.
        ValueError: If the profile is unknown or the API key is missing.
    """
    actual_nonce = nonce or secrets.token_hex(16)
    tool_name = "incidentlens_canary"

    def canary_tool(nonce: str) -> str:
        """Echo back the nonce for audit verification."""
        return nonce

    tool = StructuredTool.from_function(
        coroutine=canary_tool,
        name=tool_name,
        description="Echo back the provided nonce for audit verification.",
        args_schema=CanaryArgs,
    )

    model = registry.get(profile_name)
    model_with_tools = model.bind_tools([tool], tool_choice="required")

    messages = [
        HumanMessage(
            content=f"Call the {tool_name} tool with nonce: {actual_nonce}"
        )
    ]

    response: AIMessage = await model_with_tools.ainvoke(messages)

    if not response.tool_calls:
        raise AssertionError(
            f"Model did not return any tool calls. Response: {response.content}"
        )

    call = response.tool_calls[0]
    if call["name"] != tool_name:
        raise AssertionError(
            f"Expected tool call to '{tool_name}', got '{call['name']}'"
        )

    returned_nonce = call["args"].get("nonce")
    if returned_nonce != actual_nonce:
        raise AssertionError(
            f"Nonce mismatch: expected '{actual_nonce}', got '{returned_nonce}'"
        )

    identity = registry.identity(profile_name)

    return CanaryResult(
        nonce=actual_nonce,
        tool_name=tool_name,
        audit_nonce=returned_nonce,
        identity=identity,
        fallback_used=False,
    )


async def run_schema_canary(
    registry: ModelRegistry,
    profile_name: str,
) -> SchemaCanaryResult:
    """Run a schema-based canary to verify the model can call structured tools.

    Tests two capabilities:
    1. Normal tool call (echo nonce)
    2. Schema-constrained tool call (RootCauseProposal-like structure)

    Args:
        registry: The model registry to get the chat model from.
        profile_name: The profile name to test.

    Returns:
        A SchemaCanaryResult with pass/fail for each capability.
    """
    identity = registry.identity(profile_name)
    model = registry.get(profile_name)

    # Test 1: Normal tool call
    nonce = secrets.token_hex(8)
    normal_tool = StructuredTool.from_function(
        coroutine=lambda nonce: nonce,
        name="echo_nonce",
        description="Echo back the nonce",
        args_schema=CanaryArgs,
    )
    normal_model = model.bind_tools([normal_tool], tool_choice="required")
    normal_response: AIMessage = await normal_model.ainvoke([
        HumanMessage(content=f"Call echo_nonce with nonce: {nonce}")
    ])
    normal_passed = (
        bool(normal_response.tool_calls)
        and normal_response.tool_calls[0]["name"] == "echo_nonce"
        and normal_response.tool_calls[0]["args"].get("nonce") == nonce
    )

    # Test 2: Schema tool call (proposal-like)
    proposal_tool = StructuredTool.from_function(
        coroutine=lambda **kw: str(kw),
        name="RootCauseProposal",
        description="Submit a root cause proposal",
        args_schema=ProposalCanaryArgs,
    )
    proposal_model = model.bind_tools([proposal_tool], tool_choice="required")
    proposal_response: AIMessage = await proposal_model.ainvoke([
        HumanMessage(
            content=(
                "Call RootCauseProposal with root_service='payment-service', "
                "cause_code='payment_latency_spike', evidence_ids=['ev-canary'], "
                "confidence=0.9, next_action='finish'"
            )
        )
    ])
    proposal_passed = (
        bool(proposal_response.tool_calls)
        and proposal_response.tool_calls[0]["name"] == "RootCauseProposal"
    )

    return SchemaCanaryResult(
        normal_tool_call_passed=normal_passed,
        proposal_tool_call_passed=proposal_passed,
        fallback_used=False,
        identity=identity,
    )
