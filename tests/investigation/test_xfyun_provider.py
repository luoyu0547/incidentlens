"""讯飞 MaaS Provider 的纯本地适配测试。"""

import json
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from incidentlens_control_plane.investigation.provider import (
    PromptTooLongError,
    ProviderError,
)
from incidentlens_control_plane.investigation.types import (
    MessageRole,
    TextBlock,
    ToolCallStatus,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
)
from incidentlens_control_plane.investigation.xfyun_provider import (
    XfyunMaaSConfig,
    XfyunMaaSProvider,
    _message_payload,
    _normalise_optional_fields,
)

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def provider_config() -> XfyunMaaSConfig:
    return XfyunMaaSConfig(
        api_key="test-key",
        base_url="https://maas.example.com/v1",
        model="spark-x",
    )


def http_error(status: int) -> HTTPError:
    return HTTPError(
        url="https://maas.example.com/v1/chat/completions",
        code=status,
        msg="error",
        hdrs={},
        fp=None,
    )


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _transcript(role: MessageRole, blocks: tuple) -> TranscriptMessage:
    return TranscriptMessage(
        agent_run_id="run-1",
        sequence=1,
        role=role,
        blocks=blocks,
        created_at=NOW,
    )


def test_http_413_is_prompt_too_long(provider_config) -> None:
    provider = XfyunMaaSProvider(provider_config)
    with patch(
        "incidentlens_control_plane.investigation.xfyun_provider.urlopen",
        side_effect=http_error(413),
    ):
        with pytest.raises(PromptTooLongError):
            provider._post({"messages": []})


def test_context_length_exceeded_body_is_prompt_too_long(provider_config) -> None:
    provider = XfyunMaaSProvider(provider_config)
    body = json.dumps(
        {"error": {"code": "context_length_exceeded", "message": "context too long"}}
    ).encode("utf-8")
    with patch(
        "incidentlens_control_plane.investigation.xfyun_provider.urlopen",
        return_value=_FakeResponse(body),
    ):
        with pytest.raises(PromptTooLongError):
            provider._post({"messages": []})


def test_retryable_http_error_is_provider_error(provider_config) -> None:
    provider = XfyunMaaSProvider(provider_config)
    with patch(
        "incidentlens_control_plane.investigation.xfyun_provider.urlopen",
        side_effect=http_error(429),
    ):
        with pytest.raises(ProviderError) as excinfo:
            provider._post({"messages": []})
    assert excinfo.value.retryable is True


def test_message_payload_maps_tool_use_to_assistant_content() -> None:
    message = _transcript(
        MessageRole.ASSISTANT,
        (
            TextBlock(text="inspecting"),
            ToolUseBlock(
                tool_call_id="call-1",
                tool_name="registry_info",
                arguments={"namespace": "default"},
            ),
        ),
    )

    payload = _message_payload(message)

    assert payload["role"] == "assistant"
    assert payload["content"] == [
        {"type": "text", "text": "inspecting"},
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "registry_info",
            "input": {"namespace": "default"},
        },
    ]


def test_message_payload_maps_tool_result_to_user_content() -> None:
    message = _transcript(
        MessageRole.USER,
        (
            TextBlock(text="the result is in"),
            ToolResultBlock(
                tool_call_id="call-1",
                status=ToolCallStatus.SUCCEEDED,
                content="service orders",
                evidence_ids=("ev-1",),
            ),
        ),
    )

    payload = _message_payload(message)

    assert payload["role"] == "user"
    assert payload["content"] == [
        {"type": "text", "text": "the result is in"},
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "content": "service orders",
            "evidence_ids": ("ev-1",),
        },
    ]


def test_normalise_optional_fields_only_converts_known_empty_shapes():
    payload = {
        "child_delegation": [],
        "stop_signal": {"stop_reason": "missing_evidence"},
        "tool_requests": [],
    }

    _normalise_optional_fields(payload)

    assert payload["child_delegation"] is None
    assert payload["stop_signal"] == {
        "stop_reason": "missing_evidence",
        "summary": "模型请求停止：missing_evidence",
    }
    assert payload["tool_requests"] == []


def test_normalise_optional_fields_rejects_non_object_result():
    try:
        _normalise_optional_fields([])
    except ValueError as exc:
        assert "must be an object" in str(exc)
    else:
        raise AssertionError("non-object provider result must be rejected")
