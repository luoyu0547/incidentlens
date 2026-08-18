"""Tests for the tool-free XFYUN MaaS compactor adapter.

Covers: payload construction (no executable tools), strict SessionMemory
response parsing, malformed/empty provider shapes, transport error redaction,
code-fence stripping, bounded JSON assertion, and request self-containedness.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest
from incidentlens_control_plane.investigation.compactor import (
    CompactionRejected,
    CompactionRequest,
)
from incidentlens_control_plane.investigation.types import (
    MessageRole,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
)
from incidentlens_control_plane.investigation.xfyun_compactor import (
    XfyunMaaSCompactor,
    _strip_fence,
)
from incidentlens_control_plane.investigation.xfyun_provider import XfyunMaaSConfig

NOW = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def http_error(status: int) -> HTTPError:
    return HTTPError(
        url="https://maas.example.com/v1/chat/completions",
        code=status,
        msg="error",
        hdrs={},
        fp=None,
    )


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *args: object) -> None:
        pass


def _response(payload: dict[str, object]) -> dict[str, object]:
    """Wrap a SessionMemory-like dict in a fake MaaS response envelope."""
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(payload, default=str),
                },
            }
        ]
    }


def _full_memory_payload() -> dict[str, object]:
    """A complete SessionMemory payload that satisfies strict validation."""
    return {
        "memory_id": "mem-run-1-1",
        "agent_run_id": "run-1",
        "investigation_id": "inv-1",
        "revision": 1,
        "through_round": 1,
        "through_transcript_sequence": 1,
        "objective": "find the root cause of checkout 502s",
        "confirmed_facts": ["checkout requests return 502 after retries"],
        "active_hypotheses": ["payment gateway timeout"],
        "open_questions": ["when did 502s start?"],
        "completed_actions": ["checked order-service logs"],
        "child_findings": [],
        "evidence_ids": [],
        "user_constraints": [],
        "todos": ["verify payment gateway health"],
        "next_actions": ["check payment gateway SLAs"],
        "created_at": NOW.isoformat(),
    }


def _sample_transcript() -> tuple[TranscriptMessage, ...]:
    return (
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=1,
            role=MessageRole.ASSISTANT,
            blocks=[
                ToolUseBlock(
                    tool_call_id="call-1",
                    tool_name="search_logs",
                    arguments={"query": "502"},
                ),
            ],
            created_at=NOW,
        ),
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=2,
            role=MessageRole.USER,
            blocks=[
                ToolResultBlock(
                    tool_call_id="call-1",
                    status="succeeded",
                    content="Found 3 matches",
                    evidence_ids=("ev-1",),
                ),
            ],
            created_at=NOW,
        ),
        TranscriptMessage(
            agent_run_id="run-1",
            sequence=3,
            role=MessageRole.ASSISTANT,
            blocks=[
                TextBlock(text="The root cause is a payment gateway timeout."),
            ],
            created_at=NOW,
        ),
    )


@pytest.fixture
def config() -> XfyunMaaSConfig:
    return XfyunMaaSConfig(
        api_key="test-key",
        base_url="https://maas.example.com/v1",
        model="spark-x",
    )


@pytest.fixture
def compact_request() -> CompactionRequest:
    return CompactionRequest(
        agent_run_id="run-1",
        investigation_id="inv-1",
        through_round=1,
        through_sequence=3,
        messages=_sample_transcript(),
        allowed_evidence_ids=("ev-1",),
    )


# ---------------------------------------------------------------------------
# Brief Step 1: adapter contract tests with mocked HTTP boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compactor_sends_no_executable_tools(
    config: XfyunMaaSConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    compactor = XfyunMaaSCompactor(config)
    with patch.object(compactor, "_post", return_value=_response(memory_payload)) as post:
        memory = await compactor.compact(compact_request)
    payload = post.call_args.args[0]
    assert payload.get("tools", []) == []
    assert payload["response_format"] == {"type": "json_object"}
    assert memory.agent_run_id == compact_request.agent_run_id


@pytest.mark.asyncio
async def test_compactor_rejects_malformed_provider_shape(
    config: XfyunMaaSConfig, compact_request: CompactionRequest
) -> None:
    compactor = XfyunMaaSCompactor(config)
    with patch.object(compactor, "_post", return_value={"choices": []}):
        with pytest.raises(CompactionRejected, match="invalid"):
            await compactor.compact(compact_request)


# ---------------------------------------------------------------------------
# Strict identity payload: the adapter never repairs model output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compactor_does_not_repair_wrong_identity(
    config: XfyunMaaSConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    """A wrong echoed identity must pass through unchanged for the manager to reject."""
    memory_payload = {**memory_payload, "agent_run_id": "other-run"}
    compactor = XfyunMaaSCompactor(config)
    with patch.object(compactor, "_post", return_value=_response(memory_payload)):
        memory = await compactor.compact(compact_request)
    assert memory.agent_run_id == "other-run"


@pytest.mark.asyncio
async def test_compactor_does_not_repair_foreign_evidence(
    config: XfyunMaaSConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    """Foreign evidence is not filtered by the adapter; the validator rejects it."""
    memory_payload = {**memory_payload, "evidence_ids": ["foreign"]}
    compactor = XfyunMaaSCompactor(config)
    with patch.object(compactor, "_post", return_value=_response(memory_payload)):
        memory = await compactor.compact(compact_request)
    assert memory.evidence_ids == ("foreign",)


@pytest.mark.asyncio
async def test_compactor_requires_full_strict_memory_shape(
    config: XfyunMaaSConfig,
    compact_request: CompactionRequest,
    memory_payload: dict[str, object],
) -> None:
    """A payload missing a mandatory semantic field is rejected, never filled in."""
    del memory_payload["objective"]
    compactor = XfyunMaaSCompactor(config)
    with patch.object(compactor, "_post", return_value=_response(memory_payload)):
        with pytest.raises(CompactionRejected, match="invalid"):
            await compactor.compact(compact_request)


# ---------------------------------------------------------------------------
# Brief Step 3: CompactionRequest self-containedness
# ---------------------------------------------------------------------------


def test_compaction_request_carries_investigation_identity(
    compact_request: CompactionRequest,
) -> None:
    """The request carries investigation_id and through_round for the compactor."""
    assert compact_request.investigation_id == "inv-1"
    assert compact_request.through_round == 1
    assert compact_request.through_sequence == 3
    assert compact_request.agent_run_id == "run-1"


# ---------------------------------------------------------------------------
# Brief Step 5: transport + redaction
# ---------------------------------------------------------------------------


def test_compactor_maps_429_to_rejected_without_response_body(
    config: XfyunMaaSConfig,
) -> None:
    compactor = XfyunMaaSCompactor(config)
    with patch(
        "incidentlens_control_plane.investigation.xfyun_compactor.urlopen",
        side_effect=http_error(429),
    ):
        with pytest.raises(CompactionRejected) as excinfo:
            compactor._post({"messages": []})
    assert "secret provider body" not in str(excinfo.value)
    assert "429" in str(excinfo.value)


def test_compactor_maps_connection_failure_to_rejected(config: XfyunMaaSConfig) -> None:
    compactor = XfyunMaaSCompactor(config)
    with patch(
        "incidentlens_control_plane.investigation.xfyun_compactor.urlopen",
        side_effect=URLError("secret connection detail"),
    ):
        with pytest.raises(CompactionRejected) as excinfo:
            compactor._post({"messages": []})
    assert "secret connection detail" not in str(excinfo.value)
    assert "connection failed" in str(excinfo.value)


# ---------------------------------------------------------------------------
# _strip_fence
# ---------------------------------------------------------------------------


def test_strip_fence_removes_markdown_wrapper() -> None:
    raw = '```json\n{"key": "value"}\n```'
    assert _strip_fence(raw) == '{"key": "value"}'


def test_strip_fence_passes_through_plain_json() -> None:
    raw = '{"key": "value"}'
    assert _strip_fence(raw) == '{"key": "value"}'


def test_strip_fence_rejects_non_string() -> None:
    with pytest.raises(CompactionRejected, match="string"):
        _strip_fence(42)


# ---------------------------------------------------------------------------
# Payload structure
# ---------------------------------------------------------------------------


def test_compaction_payload_has_system_and_user_messages(
    config: XfyunMaaSConfig,
    compact_request: CompactionRequest,
) -> None:
    from incidentlens_control_plane.investigation.xfyun_compactor import (
        _compaction_messages,
    )

    messages = _compaction_messages(compact_request)
    roles = [m["role"] for m in messages]
    assert roles[0] == "system"
    assert all(r == "user" for r in roles[1:])


def test_expected_output_includes_identity_fields(
    config: XfyunMaaSConfig,
    compact_request: CompactionRequest,
) -> None:
    from incidentlens_control_plane.investigation.xfyun_compactor import (
        _serialize_expected_output,
    )

    expected = json.loads(_serialize_expected_output(compact_request))
    assert expected["agent_run_id"] == "run-1"
    assert expected["investigation_id"] == "inv-1"
    assert expected["through_round"] == 1
    assert expected["through_transcript_sequence"] == 3
    assert expected["expected_revision"] == 1  # no prior memory


@pytest.fixture
def memory_payload() -> dict[str, object]:
    return _full_memory_payload()
