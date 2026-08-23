"""Tool-free ContextCompactor for OpenAI-compatible APIs.

Implements ``ContextCompactor`` by calling the configured OpenAI-compatible model with a
text-only instruction and an empty tool list.  The compactor never executes
operations, delegates children, requests approval, or invents evidence.

``CompactionValidator`` remains the only authority that accepts or rejects the
returned ``SessionMemory``.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from pydantic import ValidationError

from incidentlens_control_plane.investigation.compactor import (
    CompactionRejected,
    CompactionRequest,
    ContextCompactor,
)
from incidentlens_control_plane.investigation.model_transport import (
    ModelTransportError,
    OpenAICompatibleConfig,
    OpenAICompatibleTransport,
)
from incidentlens_control_plane.investigation.provider import PromptTooLongError
from incidentlens_control_plane.investigation.state_machine import ToolCallStatus
from incidentlens_control_plane.investigation.types import (
    SessionMemory,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
)


class OpenAICompatibleCompactor(ContextCompactor):
    """OpenAI-compatible compactor that sends no executable tools."""

    def __init__(
        self, config: OpenAICompatibleConfig, transport: OpenAICompatibleTransport
    ) -> None:
        self._config = config
        self._transport = transport

    async def compact(self, request: CompactionRequest) -> SessionMemory:
        payload: dict[str, Any] = {
            "model": self._config.model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": _compaction_messages(request),
            "tools": [],
        }
        try:
            response = await asyncio.to_thread(
                self._transport.chat_completions, payload
            )
        except PromptTooLongError as exc:
            raise CompactionRejected("model compaction context is too long") from exc
        except ModelTransportError as exc:
            raise CompactionRejected(exc.message) from exc
        try:
            content = response["choices"][0]["message"]["content"]
            memory = SessionMemory.model_validate_json(_strip_fence(content))
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            raise CompactionRejected(
                "model compaction response is invalid"
            ) from exc
        _require_preserved_state(request, memory)
        return memory


def _compaction_messages(request: CompactionRequest) -> list[dict[str, object]]:
    """Build the chat-completion message list for a compaction request."""
    prior_section = _serialize_prior_memory(request)
    transcript_section = _serialize_transcript(request.messages)
    expected = _serialize_expected_output(request)
    return [
        {"role": "system", "content": _COMPACTION_SYSTEM_PROMPT},
        {"role": "user", "content": prior_section},
        {"role": "user", "content": transcript_section},
        {"role": "user", "content": expected},
    ]


def _serialize_prior_memory(request: CompactionRequest) -> str:
    """Serialize the prior memory (if any) to bounded JSON text."""
    if request.prior_memory is None:
        return json.dumps({"prior_memory": None}, ensure_ascii=False)
    return json.dumps(
        {"prior_memory": request.prior_memory.model_dump(mode="json")},
        ensure_ascii=False,
        default=str,
    )


def _serialize_transcript(messages: tuple[TranscriptMessage, ...]) -> str:
    """Serialize transcript messages to bounded JSON text."""
    payload: list[dict[str, Any]] = []
    for message in messages:
        entry: dict[str, Any] = {
            "sequence": message.sequence,
            "role": message.role.value,
            "blocks": [],
        }
        for block in message.blocks:
            if isinstance(block, TextBlock):
                entry["blocks"].append({"type": "text", "text": block.text})
            elif isinstance(block, ToolUseBlock):
                entry["blocks"].append({
                    "type": "tool_use",
                    "tool_call_id": block.tool_call_id,
                    "tool_name": block.tool_name,
                    "arguments": block.arguments,
                })
            elif isinstance(block, ToolResultBlock):
                result: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_call_id": block.tool_call_id,
                    "status": block.status.value,
                    "content": block.content,
                }
                if block.evidence_ids:
                    result["evidence_ids"] = list(block.evidence_ids)
                entry["blocks"].append(result)
        payload.append(entry)
    text = json.dumps(
        {"transcript": payload},
        ensure_ascii=False,
        default=str,
    )
    _assert_bounded_json(text, max_chars=200_000)
    return text


def _serialize_expected_output(request: CompactionRequest) -> str:
    """Instruct the model on the exact identity fields to echo."""
    expected_revision = (
        (request.prior_memory.revision + 1) if request.prior_memory is not None else 1
    )
    return json.dumps(
        {
            "instructions": (
                "Summarize the transcript into a SessionMemory object. "
                "You must echo the following identity fields exactly: "
                "agent_run_id, investigation_id, revision, "
                "through_round, through_transcript_sequence. "
                "Preserve every evidence-backed SHA-256 hash verbatim. "
                "Carry every pending approval and proposed change in "
                "safety_state and pending_actions. "
                "Record the latest verification outcome (applied / verified / "
                "failed / rolled back / reapplied) in completed_actions or "
                "open_questions. "
                "Always end with concrete next_actions."
            ),
            "agent_run_id": request.agent_run_id,
            "investigation_id": request.investigation_id,
            "expected_revision": expected_revision,
            "through_round": request.through_round,
            "through_transcript_sequence": request.through_sequence,
            "allowed_evidence_ids": list(request.allowed_evidence_ids),
        },
        ensure_ascii=False,
    )


def _assert_bounded_json(text: str, *, max_chars: int) -> None:
    """Raise CompactionRejected if serialized text exceeds the bound."""
    if len(text) > max_chars:
        raise CompactionRejected(
            f"compaction transcript payload exceeds {max_chars} chars"
        )


def _strip_fence(content: object) -> str:
    """Strip markdown code-fence wrapping from a model response."""
    if not isinstance(content, str):
        raise CompactionRejected("message.content must be a string")
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        return text.split("\n", 1)[1].rsplit("\n", 1)[0]
    return text


_COMPACTION_SYSTEM_PROMPT = """\
You are a context compaction assistant. Your ONLY task is to produce a \
SessionMemory JSON object that summarizes the given transcript.

Return exactly one JSON object matching the SessionMemory schema:
- memory_id: a unique string identifier (use the provided value if given)
- agent_run_id: echo the provided value exactly
- investigation_id: echo the provided value exactly
- revision: echo the expected_revision value exactly
- through_round: echo the provided value exactly
- through_transcript_sequence: echo the provided value exactly
- objective: a concise summary of what is being investigated
- confirmed_facts: list of established facts (short, redacted strings)
- active_hypotheses: list of current hypotheses
- rejected_hypotheses: rejected hypotheses with their reason
- open_questions: list of unresolved questions
- completed_actions: list of completed investigation actions, including the
  latest verification outcome
- child_findings: list of child investigation findings
- reacquisition_recipes: reproducible remote observations as objects with
  purpose, tool_name, redacted arguments, and stale_summary
- immutable_observations: bounded pre-change, rotated, transient, or one-time
  observations; preserve every evidence-backed SHA-256 hash verbatim
- pending_actions: pending approvals, repairs, verification, rollback, or
  reapply work; carry every tool call that is still waiting approval or that
  failed / ran uncertain
- safety_state: approval, changeset, backup, uncertain execution, verification
  and recovery state; never drop a pending approval or an unverified change
- evidence_ids: only from the allowed_evidence_ids list; never fabricate
- user_constraints: list of user-stated constraints
- todos: list of pending investigation items
- next_actions: list of recommended next steps (always concrete and non-empty)
- created_at: ISO timestamp

Never invent evidence IDs not in the allowed list.
Never change the agent_run_id, investigation_id, or through_round values.
Never change the through_transcript_sequence to a value lower than requested.
Preserve every evidence-backed SHA-256 hash exactly.
Preserve every pending approval / proposed / applied / unverified change.
Record the latest verification outcome in completed_actions or open_questions.
Always end with concrete next actions.
Return ONLY the JSON object, no markdown fences, no explanation.
"""


_SHA256_TOKEN_RE = re.compile(r"\b[0-9a-f]{64}\b")
_VERIFICATION_MARKERS = ("verified", "verification", "verify", "rollback", "reapplied")


def _require_preserved_state(request: CompactionRequest, memory: SessionMemory) -> None:
    """Reject a memory that drops incident state the transcript still carries.

    The compaction contract is a *preservation* contract: evidence-backed
    SHA-256 hashes, pending approvals / unverified changes, the latest
    verification outcome and concrete next actions must survive the summary.
    Each check scans the bounded transcript the compactor was asked to
    summarize and requires the corresponding existing ``SessionMemory`` field
    (never a new schema field) to carry that state.
    """
    if not memory.next_actions:
        raise CompactionRejected(
            "model compaction memory must preserve concrete next_actions"
        )

    transcript_hashes: set[str] = set()
    pending_calls: set[str] = set()
    has_verification = False
    for message in request.messages:
        for block in message.blocks:
            if isinstance(block, ToolResultBlock):
                if block.status in {
                    ToolCallStatus.WAITING_APPROVAL,
                    ToolCallStatus.FAILED,
                    ToolCallStatus.UNCERTAIN,
                }:
                    pending_calls.add(block.tool_call_id)
                transcript_hashes.update(_SHA256_TOKEN_RE.findall(block.content))
                if any(marker in block.content.lower() for marker in _VERIFICATION_MARKERS):
                    has_verification = True
            elif isinstance(block, ToolUseBlock):
                if "verify" in block.tool_name.lower():
                    has_verification = True
            elif isinstance(block, TextBlock):
                transcript_hashes.update(_SHA256_TOKEN_RE.findall(block.text))
                if any(marker in block.text.lower() for marker in _VERIFICATION_MARKERS):
                    has_verification = True

    carried = " ".join(_memory_text_fields(memory))
    dropped_hashes = sorted(
        transcript_hash for transcript_hash in transcript_hashes if transcript_hash not in carried
    )
    if dropped_hashes:
        raise CompactionRejected(
            "model compaction memory drops evidence-backed hash"
            f" {dropped_hashes[0]!r}"
        )

    safety_text = " ".join((*memory.safety_state, *memory.pending_actions))
    dropped_calls = sorted(call_id for call_id in pending_calls if call_id not in safety_text)
    if dropped_calls:
        raise CompactionRejected(
            "model compaction memory drops pending state for call"
            f" {dropped_calls[0]!r}"
        )

    if has_verification:
        verification_text = " ".join((*memory.completed_actions, *memory.open_questions)).lower()
        if not verification_text or not any(
            marker in verification_text for marker in _VERIFICATION_MARKERS
        ):
            raise CompactionRejected(
                "model compaction memory drops the latest verification state"
            )


def _memory_text_fields(memory: SessionMemory) -> list[str]:
    """Every free-text value in a ``SessionMemory`` (evidence ids are ids)."""
    fields: list[str] = []
    for field, value in memory.model_dump(mode="json").items():
        if field == "evidence_ids":
            continue
        if isinstance(value, str):
            fields.append(value)
        elif isinstance(value, list):
            fields.extend(item for item in value if isinstance(item, str))
            fields.extend(
                str(nested)
                for item in value
                if isinstance(item, dict)
                for nested in item.values()
                if isinstance(nested, str)
            )
    return fields


__all__ = [
    "OpenAICompatibleCompactor",
    "OpenAICompatibleConfig",
]
