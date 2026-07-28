"""Live Canary for verifying model tool-calling capability."""
import secrets
from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .registry import ModelIdentity, ModelRegistry


class CanaryArgs(BaseModel):
    """Input schema for the canary tool."""
    nonce: str = Field(description="A random nonce to echo back for audit verification")


@dataclass(frozen=True)
class CanaryResult:
    """Result of a successful canary test."""
    nonce: str
    tool_name: str
    audit_nonce: str
    identity: ModelIdentity
    fallback_used: bool


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
