"""Tool-free MaaS ContextCompactor adapter for XFYUN MaaS.

Implements ``ContextCompactor`` by calling the configured MaaS model with a
text-only instruction and an empty tool list.  The compactor never executes
operations, delegates children, requests approval, or invents evidence.

``CompactionValidator`` remains the only authority that accepts or rejects the
returned ``SessionMemory``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import ValidationError

from incidentlens_control_plane.investigation.compactor import (
    CompactionRejected,
    CompactionRequest,
    ContextCompactor,
)
from incidentlens_control_plane.investigation.types import (
    SessionMemory,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
)


@dataclass(frozen=True, slots=True)
class XfyunMaaSConfig:
    """MaaS OpenAI-compatible endpoint connection configuration.

    Shares the same shape as ``xfyun_provider.XfyunMaaSConfig`` but is
    defined here to keep the compactor module self-contained.
    """

    api_key: str
    base_url: str
    model: str
    timeout_seconds: float = 90.0


class XfyunMaaSCompactor(ContextCompactor):
    """MaaS-backed compactor that sends no executable tools."""

    def __init__(self, config: XfyunMaaSConfig) -> None:
        self._config = config

    async def compact(self, request: CompactionRequest) -> SessionMemory:
        payload: dict[str, Any] = {
            "model": self._config.model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": _compaction_messages(request),
            "tools": [],
        }
        response = await asyncio.to_thread(self._post, payload)
        try:
            content = response["choices"][0]["message"]["content"]
            return SessionMemory.model_validate_json(_strip_fence(content))
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            raise CompactionRejected(
                "MaaS compaction response is invalid"
            ) from exc

    def _post(self, payload: dict[str, object]) -> dict[str, object]:
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._config.timeout_seconds) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise CompactionRejected(
                f"MaaS compaction request failed (HTTP {exc.code})"
            ) from exc
        except (TimeoutError, URLError) as exc:
            raise CompactionRejected("MaaS compaction connection failed") from exc


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
                "through_round, through_transcript_sequence."
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
- open_questions: list of unresolved questions
- completed_actions: list of completed investigation actions
- child_findings: list of child investigation findings
- evidence_ids: only from the allowed_evidence_ids list; never fabricate
- user_constraints: list of user-stated constraints
- todos: list of pending investigation items
- next_actions: list of recommended next steps
- created_at: ISO timestamp

Never invent evidence IDs not in the allowed list.
Never change the agent_run_id, investigation_id, or through_round values.
Never change the through_transcript_sequence to a value lower than requested.
Return ONLY the JSON object, no markdown fences, no explanation.
"""


__all__ = [
    "XfyunMaaSCompactor",
    "XfyunMaaSConfig",
]
