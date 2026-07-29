"""Live Canary for verifying model tool-calling and conclusion capability."""
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


# ---------------------------------------------------------------------------
# Conclusion schema canary
# ---------------------------------------------------------------------------


class ConclusionCanaryArgs(BaseModel):
    """Input schema for the conclusion canary tool."""
    root_service: str = Field(description="The service responsible for the incident")
    cause_code: str = Field(description="The cause code from the eligible set")
    evidence_ids: list[str] = Field(description="Evidence IDs supporting the conclusion")
    confidence: float = Field(ge=0, le=1, description="Confidence in the conclusion")
    next_action: str = Field(description="Either 'finish' or 'needs_more_evidence'")


@dataclass(frozen=True)
class ConclusionCanaryResult:
    """Result of a successful conclusion canary test."""
    root_service: str
    cause_code: str
    evidence_ids: list[str]
    confidence: float
    next_action: str
    identity: ModelIdentity
    fallback_used: bool


async def run_conclusion_canary(
    registry: ModelRegistry,
    profile_name: str,
) -> ConclusionCanaryResult:
    """Run a live canary test to verify a model can emit a RootCauseProposal.

    This function:
    - binds only a synthetic proposal tool
    - requires a tool call
    - validates all proposal fields with Pydantic
    - uses synthetic Evidence IDs supplied in the prompt
    - records only redacted identity and pass/fail metadata

    This canary tests provider capability without asserting an incident root cause.

    Args:
        registry: The model registry to get the chat model from.
        profile_name: The profile name to test.

    Returns:
        A ConclusionCanaryResult with the parsed proposal fields and identity.

    Raises:
        AssertionError: If the model fails to emit a valid proposal.
    """
    tool = StructuredTool.from_function(
        coroutine=lambda **kwargs: kwargs,
        name="root_cause_proposal",
        description="Emit a structured root cause proposal for the incident.",
        args_schema=ConclusionCanaryArgs,
    )

    model = registry.get(profile_name)
    model_with_tools = model.bind_tools([tool], tool_choice="required")

    # Synthetic evidence IDs for the canary
    synthetic_evidence_ids = ["canary-ev-001", "canary-ev-002"]

    messages = [
        HumanMessage(
            content=(
                "You are in the CONCLUSION phase of an incident investigation.\n\n"
                "Eligible cause codes: payment_latency_spike\n"
                f"Eligible Evidence IDs: {', '.join(synthetic_evidence_ids)}\n\n"
                "Emit a RootCauseProposal choosing from the eligible set. "
                "Cite only the eligible Evidence IDs."
            )
        )
    ]

    response: AIMessage = await model_with_tools.ainvoke(messages)

    if not response.tool_calls:
        raise AssertionError(
            f"Model did not return any tool calls. Response: {response.content}"
        )

    call = response.tool_calls[0]
    if call["name"] != "root_cause_proposal":
        raise AssertionError(
            f"Expected tool call to 'root_cause_proposal', got '{call['name']}'"
        )

    args = call["args"]

    # Validate with Pydantic
    try:
        proposal = ConclusionCanaryArgs.model_validate(args)
    except Exception as exc:
        raise AssertionError(
            f"Proposal validation failed: {exc}. Raw args: {args}"
        ) from exc

    # Validate evidence IDs are from the synthetic set
    for eid in proposal.evidence_ids:
        if eid not in synthetic_evidence_ids:
            raise AssertionError(
                f"Evidence ID '{eid}' not in synthetic set: {synthetic_evidence_ids}"
            )

    # Validate next_action
    if proposal.next_action not in ("finish", "needs_more_evidence"):
        raise AssertionError(
            f"Invalid next_action: '{proposal.next_action}'"
        )

    identity = registry.identity(profile_name)

    return ConclusionCanaryResult(
        root_service=proposal.root_service,
        cause_code=proposal.cause_code,
        evidence_ids=proposal.evidence_ids,
        confidence=proposal.confidence,
        next_action=proposal.next_action,
        identity=identity,
        fallback_used=False,
    )
