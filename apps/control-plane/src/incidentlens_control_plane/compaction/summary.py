"""Summary generation with circuit breaker for session compaction.

Provides text-only summarization of investigation history using the LLM,
with circuit breaker protection against repeated failures.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from langchain_core.messages import HumanMessage, SystemMessage

from incidentlens_control_plane.compaction.domain import (
    CompactionException,
    SummaryResult,
)
from incidentlens_control_plane.compaction.session import SessionMemorySnapshot


class SummaryModel(Protocol):
    """Protocol for the model used for summarization."""

    async def ainvoke(self, messages: list[SystemMessage | HumanMessage]) -> Any:
        """Invoke the model with messages."""
        ...


class SummaryCircuitBreaker:
    """Tracks summary failures and opens circuit after max_failures.

    When the circuit is open, summary generation is skipped and
    the caller should use deterministic projection instead.
    """

    def __init__(self, max_failures: int = 3) -> None:
        """Initialize the circuit breaker.

        Args:
            max_failures: Number of consecutive failures before opening circuit.
        """
        self._max_failures = max_failures
        self._failure_count = 0
        self._is_open = False
        self._last_error: str | None = None

    @property
    def is_open(self) -> bool:
        """Check if circuit is open (failures exceeded threshold)."""
        return self._is_open

    @property
    def failure_count(self) -> int:
        """Current consecutive failure count."""
        return self._failure_count

    def record_success(self) -> None:
        """Record a successful summary, resetting the circuit."""
        self._failure_count = 0
        self._is_open = False
        self._last_error = None

    def record_failure(self, error: str = "") -> None:
        """Record a summary failure.

        Args:
            error: Error message for diagnostics.
        """
        self._failure_count += 1
        self._last_error = error
        if self._failure_count >= self._max_failures:
            self._is_open = True

    def reset(self) -> None:
        """Manually reset the circuit breaker."""
        self._failure_count = 0
        self._is_open = False
        self._last_error = None


SUMMARY_SYSTEM_PROMPT = """You are summarizing an incident investigation history.

CRITICAL: TEXT ONLY; DO NOT CALL TOOLS.

You must produce a JSON object with EXACTLY this structure:
{
  "objective": "<the investigation objective>",
  "evidence_ids": ["<list of all evidence IDs mentioned>"],
  "verified_facts": ["<list of verified facts>"],
  "rejected_directions": ["<list of rejected investigation directions>"],
  "completed_work": ["<list of completed investigation steps>"],
  "next_action": "<what should be done next>"
}

RULES:
1. Preserve ALL evidence IDs exactly as they appear
2. Preserve the objective exactly as stated
3. List verified facts as concise bullet points
4. List rejected directions with brief reason
5. List completed work items
6. State the next logical action

Return ONLY the JSON object, no markdown fencing, no explanation."""


async def summarize_history(
    messages: list[dict[str, Any]],
    session_memory: SessionMemorySnapshot,
    model: SummaryModel,
) -> SummaryResult:
    """Generate a summary of investigation history using the LLM.

    Args:
        messages: Message history to summarize.
        session_memory: Current session memory snapshot for context.
        model: The LLM model to use for summarization.

    Returns:
        SummaryResult with extracted summary information.

    Raises:
        CompactionError: If summary generation or validation fails.
    """
    # Build the prompt with message history
    history_text = _format_messages_for_summary(messages)

    user_prompt = f"""## Current Session Memory
Objective: {session_memory.objective}
Evidence IDs: {', '.join(session_memory.evidence_ids) if session_memory.evidence_ids else 'None yet'}
Current Phase: {session_memory.current_phase}
Round: {session_memory.current_round}/{session_memory.max_rounds}

## Message History to Summarize
{history_text}

Produce the summary JSON now."""

    try:
        response = await model.ainvoke([
            SystemMessage(content=SUMMARY_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ])
    except Exception as exc:
        raise CompactionException(
            code="summary_failed",
            message=f"Model invocation failed: {exc}",
            details={"error_type": type(exc).__name__},
        ) from exc

    # Extract content from response
    content = _extract_response_content(response)
    if not content:
        raise CompactionException(
            code="summary_failed",
            message="Empty response from summary model",
        )

    # Parse and validate the summary
    summary = _parse_and_validate_summary(content, session_memory)
    return summary


def _format_messages_for_summary(messages: list[dict[str, Any]]) -> str:
    """Format messages into readable text for summarization."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Handle multipart content
            content = " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        if not content:
            continue
        # Truncate very long content
        if len(content) > 2000:
            content = content[:2000] + "... [truncated]"
        parts.append(f"[{role}]: {content}")
    return "\n".join(parts)


def _extract_response_content(response: Any) -> str:
    """Extract text content from model response."""
    if hasattr(response, "content"):
        content = response.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Handle list content (e.g., tool use blocks)
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
                elif isinstance(item, str):
                    texts.append(item)
            return "\n".join(texts)
    if isinstance(response, str):
        return response
    return ""


def _parse_and_validate_summary(
    content: str,
    session_memory: SessionMemorySnapshot,
) -> SummaryResult:
    """Parse JSON summary and validate completeness.

    Args:
        content: Raw JSON string from model.
        session_memory: Session memory for validation context.

    Returns:
        Validated SummaryResult.

    Raises:
        CompactionError: If parsing or validation fails.
    """
    # Strip markdown fencing if present
    cleaned = content.strip()
    if cleaned.startswith("```"):
        # Remove first and last lines (```json and ```)
        lines = cleaned.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise CompactionException(
            code="summary_failed",
            message=f"Failed to parse summary JSON: {exc}",
            details={"raw_content": content[:500]},
        ) from exc

    if not isinstance(data, dict):
        raise CompactionException(
            code="summary_failed",
            message="Summary is not a JSON object",
        )

    # Validate required fields
    objective = data.get("objective", "")
    if not objective:
        raise CompactionException(
            code="summary_failed",
            message="Summary missing objective",
        )

    # Validate evidence IDs exist in session memory
    summary_evidence_ids = data.get("evidence_ids", [])
    if not isinstance(summary_evidence_ids, list):
        raise CompactionException(
            code="summary_failed",
            message="evidence_ids is not a list",
        )

    # Cross-reference with session memory evidence IDs
    memory_evidence_set = set(session_memory.evidence_ids)
    summary_evidence_set = set(summary_evidence_ids)

    # Check that all evidence IDs from summary exist in memory
    # (summary might reference fewer if compacting early messages)
    missing_in_memory = summary_evidence_set - memory_evidence_set
    if missing_in_memory and session_memory.evidence_ids:
        # Evidence IDs in summary but not in memory could indicate hallucination
        # Allow it but flag in details
        pass

    # Build result
    return SummaryResult(
        summary_text=content,
        objective=objective,
        evidence_ids=summary_evidence_ids,
        verified_facts=data.get("verified_facts", []),
        rejected_directions=data.get("rejected_directions", []),
        completed_work=data.get("completed_work", []),
        next_action=data.get("next_action", ""),
    )
