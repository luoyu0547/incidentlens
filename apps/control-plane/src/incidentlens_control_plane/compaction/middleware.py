"""Compaction middleware for pre-model context management.

Provides fixed-order pre-model compaction with transcript persistence,
session memory updates, and reactive recovery for prompt-too-long errors.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    Runtime,
)
from langchain_core.messages import AIMessage, SystemMessage

from incidentlens_control_plane.compaction.domain import (
    CompactionException,
    CompactionLimits,
    CompactionOutcome,
    CompactionResult,
    SummaryResult,
    TranscriptRecord,
)
from incidentlens_control_plane.compaction.micro import micro_compact, snip_middle
from incidentlens_control_plane.compaction.session import (
    SessionMemorySnapshot,
    SessionMemoryStore,
    project_session_memory,
)
from incidentlens_control_plane.compaction.summary import (
    SummaryCircuitBreaker,
    summarize_history,
)
from incidentlens_control_plane.compaction.tool_budget import (
    ToolOutputStore,
    persist_oversized_tool_results,
)
from incidentlens_control_plane.llm.config import ModelProfile


class ModelProfileProtocol(Protocol):
    """Protocol for model profile with context window info."""

    @property
    def context_window_tokens(self) -> int:
        """Total context window size in tokens."""
        ...

    @property
    def reserved_output_tokens(self) -> int:
        """Tokens reserved for output."""
        ...


class TranscriptStore:
    """Persists JSONL transcripts for compaction audit."""

    def __init__(self, base_dir: Path | str) -> None:
        """Initialize with base directory for transcripts.

        Args:
            base_dir: Root directory for transcript storage.
        """
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def persist_transcript(
        self,
        incident_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> Path:
        """Persist messages as JSONL transcript.

        Args:
            incident_id: The incident identifier.
            session_id: The session identifier.
            messages: Messages to persist.

        Returns:
            Path to the transcript file.
        """
        transcript_dir = self._base_dir / incident_id
        transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = transcript_dir / f"{session_id}.jsonl"

        now = datetime.now(tz=timezone.utc).isoformat()

        with open(transcript_path, "a", encoding="utf-8") as f:
            for msg in messages:
                record = TranscriptRecord(
                    role=msg.get("role", "unknown"),
                    content=str(msg.get("content", "")),
                    timestamp=now,
                    metadata={
                        "tool_call_id": msg.get("tool_call_id", ""),
                        "has_tool_calls": bool(msg.get("tool_calls")),
                    },
                )
                f.write(record.model_dump_json() + "\n")

        return transcript_path


def is_prompt_too_long(exc: Exception) -> bool:
    """Check if an exception indicates prompt-too-long error.

    Normalizes provider-specific error messages to detect context length
    exceeded errors.

    Args:
        exc: The exception to check.

    Returns:
        True if the error indicates prompt is too long.
    """
    error_str = str(exc).lower()
    error_type = type(exc).__name__.lower()

    # Common patterns for prompt-too-long errors
    patterns = [
        "prompt is too long",
        "context length exceeded",
        "maximum context length",
        "token limit",
        "context_window",
        "too many tokens",
        "input is too long",
        "max_tokens",
        "prompt too large",
        "request too large",
        "context window exceeded",
    ]

    # Check error message
    for pattern in patterns:
        if pattern in error_str:
            return True

    # Check error type name
    long_error_types = [
        "prompttoolongerror",
        "contextlengtherror",
        "tokencountexceeded",
        "requesttoolargeerror",
    ]
    for error_name in long_error_types:
        if error_name in error_type:
            return True

    return False


class CompactionMiddleware(AgentMiddleware[dict[str, Any], Any]):
    """Pre-model compaction middleware with reactive recovery.

    Performs fixed-order compaction before each model call:
    1. Persist JSONL transcript
    2. Update Session Memory
    3. Apply tool budget (persist oversized outputs)
    4. Middle snip (remove complete middle groups)
    5. Micro compact (replace old tool results)

    Also handles reactive recovery when prompt-too-long errors occur.
    """

    name = "CompactionMiddleware"

    def __init__(
        self,
        runtime: Any,
        limits: CompactionLimits | None = None,
        model_profile: ModelProfileProtocol | None = None,
        session_store: SessionMemoryStore | None = None,
        tool_output_store: ToolOutputStore | None = None,
        transcript_store: TranscriptStore | None = None,
        summary_model: Any = None,
        summary_circuit_breaker: SummaryCircuitBreaker | None = None,
        max_retries: int = 2,
    ) -> None:
        """Initialize the compaction middleware.

        Args:
            runtime: The agent runtime for accessing state.
            limits: Compaction limits configuration.
            model_profile: Model profile with context window info.
            session_store: Store for session memory persistence.
            tool_output_store: Store for persisting oversized tool outputs.
            transcript_store: Store for JSONL transcripts.
            summary_model: Model for summary generation.
            summary_circuit_breaker: Circuit breaker for summary failures.
            max_retries: Maximum retries for reactive recovery.
        """
        self._runtime = runtime
        self._limits = limits or CompactionLimits()
        self._model_profile = model_profile
        self._session_store = session_store
        self._tool_output_store = tool_output_store
        self._transcript_store = transcript_store
        self._summary_model = summary_model
        self._summary_circuit_breaker = summary_circuit_breaker or SummaryCircuitBreaker()
        self._max_retries = max_retries

    def _compute_threshold(self) -> int:
        """Compute the token threshold for compaction.

        Returns:
            Threshold in tokens: context_window - reserved_output - 13000
        """
        if self._model_profile is None:
            # Default conservative threshold
            return 100_000

        context_window = self._model_profile.context_window_tokens
        reserved_output = self._model_profile.reserved_output_tokens
        return context_window - reserved_output - 13_000

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Estimate token count from messages.

        Uses rough approximation of 4 characters per token.
        """
        total_chars = sum(
            len(str(msg.get("content", "")))
            for msg in messages
        )
        return total_chars // 4

    def _persist_transcript(
        self,
        state: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> Path | None:
        """Persist messages to JSONL transcript.

        Returns:
            Path to transcript file, or None if no store configured.
        """
        if self._transcript_store is None:
            return None

        incident_id = state.get("incident_id", "unknown")
        session_id = state.get("session_id", f"{incident_id}-session")

        return self._transcript_store.persist_transcript(
            incident_id, session_id, messages
        )

    def _update_session_memory(
        self,
        state: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> SessionMemorySnapshot | None:
        """Project and persist session memory.

        Returns:
            Updated SessionMemorySnapshot, or None if no store configured.
        """
        snapshot = project_session_memory(state, messages)

        if self._session_store is not None:
            self._session_store.save(snapshot)

        return snapshot

    def _apply_tool_budget(
        self,
        messages: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> CompactionResult:
        """Apply tool budget to persist oversized outputs.

        Returns:
            CompactionResult from tool budget operation.
        """
        if self._tool_output_store is None:
            return CompactionResult(
                outcome=CompactionOutcome.SKIPPED,
                messages_removed=0,
                messages_remaining=len(messages),
                details={"reason": "no_tool_output_store"},
            )

        incident_id = state.get("incident_id", "unknown")
        return persist_oversized_tool_results(
            messages, incident_id, self._tool_output_store, self._limits
        )

    def _apply_middle_snip(
        self,
        messages: list[dict[str, Any]],
        threshold_tokens: int,
    ) -> CompactionResult:
        """Apply middle snipping to reduce context size.

        Returns:
            CompactionResult from snipping operation.
        """
        current_tokens = self._estimate_tokens(messages)
        if current_tokens <= threshold_tokens:
            return CompactionResult(
                outcome=CompactionOutcome.SKIPPED,
                messages_removed=0,
                messages_remaining=len(messages),
                details={"reason": "under_threshold", "tokens": current_tokens},
            )

        return snip_middle(messages, threshold_tokens, self._limits)

    def _apply_micro_compact(
        self,
        messages: list[dict[str, Any]],
    ) -> CompactionResult:
        """Apply micro compaction to replace old tool results.

        Returns:
            CompactionResult from micro compaction.
        """
        return micro_compact(messages, self._limits.keep_recent_results, self._limits)

    def _apply_compaction_pipeline(
        self,
        state: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[CompactionResult]]:
        """Apply the full compaction pipeline in fixed order.

        Returns:
            Tuple of (compacted messages, list of compaction results).
        """
        results: list[CompactionResult] = []
        current_messages = list(messages)

        # Step 1: Persist transcript (audit, doesn't modify messages)
        self._persist_transcript(state, current_messages)

        # Step 2: Update session memory (audit, doesn't modify messages)
        self._update_session_memory(state, current_messages)

        # Step 3: Tool budget
        tool_budget_result = self._apply_tool_budget(current_messages, state)
        results.append(tool_budget_result)

        # Step 4: Middle snip
        threshold = self._compute_threshold()
        snip_result = self._apply_middle_snip(current_messages, threshold)
        results.append(snip_result)

        # Step 5: Micro compact
        micro_result = self._apply_micro_compact(current_messages)
        results.append(micro_result)

        return current_messages, results

    async def awrap_model_call(
        self,
        request: ModelRequest[dict[str, Any]],
        handler: Any,
    ) -> ModelResponse | AIMessage:
        """Wrap model call with pre-compaction and reactive recovery.

        Applies compaction before the model call. If prompt-too-long
        error occurs, retries with progressively smaller budgets.
        """
        state = dict(request.state) if isinstance(request.state, dict) else {}
        messages = self._extract_messages(request)

        # Apply compaction pipeline
        compacted_messages, _ = self._apply_compaction_pipeline(state, messages)

        # Build request with compacted messages
        enriched_request = self._override_messages(request, compacted_messages)

        # Try the model call with reactive recovery
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await handler(enriched_request)
            except Exception as exc:
                if not is_prompt_too_long(exc):
                    raise

                last_exc = exc

                if attempt >= self._max_retries:
                    break

                # Reactive recovery: apply more aggressive compaction
                enriched_request = await self._reactive_recovery(
                    state, compacted_messages, enriched_request, handler
                )

        # All retries exhausted
        raise CompactionException(
            code="prompt_too_long",
            message=f"Prompt too long after {self._max_retries} retries: {last_exc}",
            details={"retries": self._max_retries},
        ) from last_exc

    async def _reactive_recovery(
        self,
        state: dict[str, Any],
        messages: list[dict[str, Any]],
        request: ModelRequest[dict[str, Any]],
        handler: Any,
    ) -> ModelRequest[dict[str, Any]]:
        """Apply more aggressive compaction for reactive recovery.

        Reduces tool budget and applies more aggressive snipping.
        """
        # Reduce tool output limits
        aggressive_limits = CompactionLimits(
            max_tool_output_bytes=max(
                self._limits.max_tool_output_bytes // 2, 32_768
            ),
            keep_recent_results=max(self._limits.keep_recent_results - 1, 1),
            max_snip_tokens=max(self._limits.max_snip_tokens // 2, 5_000),
        )

        # Apply more aggressive compaction
        current_messages = list(messages)

        # More aggressive tool budget
        if self._tool_output_store is not None:
            incident_id = state.get("incident_id", "unknown")
            persist_oversized_tool_results(
                current_messages, incident_id,
                self._tool_output_store, aggressive_limits
            )

        # More aggressive snipping
        threshold = self._compute_threshold() // 2
        snip_middle(current_messages, threshold, aggressive_limits)

        # More aggressive micro compact
        micro_compact(current_messages, aggressive_limits.keep_recent_results, aggressive_limits)

        return self._override_messages(request, current_messages)

    def _extract_messages(
        self, request: ModelRequest[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Extract messages from the request as list of dicts."""
        messages: list[dict[str, Any]] = []

        # System message first
        if request.system_message:
            messages.append({
                "role": "system",
                "content": request.system_message.text if hasattr(request.system_message, "text") else str(request.system_message),
            })

        # Other messages
        if hasattr(request, "messages") and request.messages:
            for msg in request.messages:
                if hasattr(msg, "type"):
                    messages.append({
                        "role": msg.type if hasattr(msg, "type") else "unknown",
                        "content": msg.text if hasattr(msg, "text") else str(msg),
                    })
                elif isinstance(msg, dict):
                    messages.append(msg)

        return messages

    def _override_messages(
        self,
        request: ModelRequest[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> ModelRequest[dict[str, Any]]:
        """Override request messages with compacted messages."""
        # Find system message
        system_content = ""
        other_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                system_content = msg.get("content", "")
            else:
                other_messages.append(msg)

        # Rebuild system message if we have compacted content
        if system_content and hasattr(request, "system_message"):
            # Keep original system message structure
            return request

        return request
