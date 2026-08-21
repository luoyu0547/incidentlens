"""OpenAI-compatible Provider 的纯本地适配测试。"""

import json
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from incidentlens_control_plane.investigation.openai_provider import (
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    _message_payload,
    _normalise_optional_fields,
    _result_payload_from_content,
    _result_payload_from_message,
)
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

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def provider_config() -> OpenAICompatibleConfig:
    return OpenAICompatibleConfig(
        api_key="test-key",
        base_url="https://llm.example.com/v1",
        model="spark-x",
    )


def http_error(status: int) -> HTTPError:
    return HTTPError(
        url="https://llm.example.com/v1/chat/completions",
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
    provider = OpenAICompatibleProvider(provider_config)
    with patch(
        "incidentlens_control_plane.investigation.openai_provider.urlopen",
        side_effect=http_error(413),
    ):
        with pytest.raises(PromptTooLongError):
            provider._post({"messages": []})


def test_context_length_exceeded_body_is_prompt_too_long(provider_config) -> None:
    provider = OpenAICompatibleProvider(provider_config)
    body = json.dumps(
        {"error": {"code": "context_length_exceeded", "message": "context too long"}}
    ).encode("utf-8")
    with patch(
        "incidentlens_control_plane.investigation.openai_provider.urlopen",
        return_value=_FakeResponse(body),
    ):
        with pytest.raises(PromptTooLongError):
            provider._post({"messages": []})


def test_retryable_http_error_is_provider_error(provider_config) -> None:
    provider = OpenAICompatibleProvider(provider_config)
    with patch(
        "incidentlens_control_plane.investigation.openai_provider.urlopen",
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
    assert json.loads(payload["content"]) == [
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
    assert json.loads(payload["content"]) == [
        {"type": "text", "text": "the result is in"},
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "content": "service orders",
            "evidence_ids": ["ev-1"],
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


def test_result_payload_unwraps_a_single_provider_result_array() -> None:
    payload = _result_payload_from_content(
        '[{"tool_requests": [], "hypotheses": [], "conclusions": [], '
        '"child_delegation": null, "stop_signal": null, "usage": {}}]'
    )

    assert payload["tool_requests"] == []


def test_result_payload_wraps_a_top_level_tool_use_array() -> None:
    payload = _result_payload_from_content(
        '[{"type":"tool_use","id":"call-1","name":"service_info",'
        '"input":{"service_name":"api-gateway"}}]'
    )

    _normalise_optional_fields(payload)

    assert payload["tool_requests"] == [
        {
            "tool_call_id": "call-1",
            "tool_name": "service_info",
            "arguments": {"service_name": "api-gateway"},
        }
    ]


def test_result_payload_wraps_a_top_level_named_tool_array() -> None:
    payload = _result_payload_from_content(
        '[{"id":"call-1","name":"service_info",'
        '"parameters":{"service_name":"api-gateway"}}]'
    )

    _normalise_optional_fields(payload)

    assert payload["tool_requests"] == [
        {
            "tool_call_id": "call-1",
            "tool_name": "service_info",
            "arguments": {"service_name": "api-gateway"},
        }
    ]


def test_result_payload_ignores_text_in_a_mixed_top_level_tool_array() -> None:
    """DeepSeek may append a text item after otherwise valid tool objects."""
    payload = _result_payload_from_content(
        '[{"type":"tool_use","id":"call-1","name":"service_info",'
        '"input":{"service_name":"api-gateway"}},'
        '{"type":"text","text":"Inspecting the target now."}]'
    )

    _normalise_optional_fields(payload)

    assert payload["tool_requests"] == [
        {
            "tool_call_id": "call-1",
            "tool_name": "service_info",
            "arguments": {"service_name": "api-gateway"},
        }
    ]


def test_result_payload_accepts_a_deepseek_dsml_only_tool_call() -> None:
    payload = _result_payload_from_content(
        "<｜｜DSML｜｜tool_calls>"
        '<｜｜DSML｜｜invoke name="service_info">'
        '<｜｜DSML｜｜parameter name="service_name" string="true">api-gateway'
        "</｜｜DSML｜｜parameter>"
        "</｜｜DSML｜｜invoke>"
        "</｜｜DSML｜｜tool_calls>"
    )

    assert payload["tool_requests"] == [
        {
            "tool_call_id": "deepseek-dsml-1",
            "tool_name": "service_info",
            "arguments": {"service_name": "api-gateway"},
        }
    ]


def test_normalise_wraps_one_top_level_openai_tool_use() -> None:
    payload = {
        "type": "tool_use",
        "id": "call-1",
        "name": "service_info",
        "input": {"service_name": "api-gateway"},
    }

    _normalise_optional_fields(payload)

    assert payload == {
        "tool_requests": [
            {
                "tool_call_id": "call-1",
                "tool_name": "service_info",
                "arguments": {"service_name": "api-gateway"},
            }
        ],
        "hypotheses": [],
        "conclusions": [],
        "child_delegation": None,
        "stop_signal": None,
    }


def test_normalise_decodes_a_stringified_todo_array() -> None:
    payload = {
        "tool_requests": [
            {
                "tool_call_id": "call-1",
                "tool_name": "todo_write",
                "arguments": {
                    "todos": '[{"todo_id":"one","content":"inspect","status":"pending"}]'
                },
            }
        ]
    }

    _normalise_optional_fields(payload)

    assert payload["tool_requests"][0]["arguments"]["todos"] == [
        {"todo_id": "one", "content": "inspect", "status": "pending"}
    ]


def test_result_payload_reads_standard_openai_tool_calls() -> None:
    payload = _result_payload_from_message(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "service_info",
                        "arguments": '{"service_name":"api-gateway"}',
                    },
                }
            ],
        }
    )

    assert payload["tool_requests"] == [
        {
            "tool_call_id": "call-1",
            "tool_name": "service_info",
            "arguments": {"service_name": "api-gateway"},
        }
    ]


def test_normalise_optional_fields_discards_model_hypothesis_title():
    payload = {
        "hypotheses": [
            {
                "hypothesis_id": "model-owned",
                "title": "Database configuration regression",
                "description": "The canary may have an invalid database port.",
                "status": "pending",
            }
        ]
    }

    _normalise_optional_fields(payload)

    assert payload["hypotheses"] == [
        {"summary": "The canary may have an invalid database port."}
    ]


def test_result_payload_accepts_deepseek_dsml_tool_call_after_json() -> None:
    payload = _result_payload_from_content(
        """{"tool_requests":[],"hypotheses":[],"conclusions":[],
        "child_delegation":null,"stop_signal":null,"usage":{}}
        <｜｜DSML｜｜tool_calls>
        <｜｜DSML｜｜invoke name="host_list">
        <｜｜DSML｜｜parameter name="service_name" string="true">api-gateway</｜｜DSML｜｜parameter>
        <｜｜DSML｜｜parameter name="path" string="true">/opt/target/config</｜｜DSML｜｜parameter>
        </｜｜DSML｜｜invoke>
        </｜｜DSML｜｜tool_calls>"""
    )

    assert payload["tool_requests"] == [
        {
            "tool_call_id": "deepseek-dsml-1",
            "tool_name": "host_list",
            "arguments": {
                "service_name": "api-gateway",
                "path": "/opt/target/config",
            },
        }
    ]


def test_normalise_optional_fields_converts_tool_use_shape() -> None:
    payload = {
        "tool_requests": [
            {
                "type": "tool_use",
                "id": "model-tool-1",
                "name": "host_list",
                "input": {"service_name": "api-gateway", "path": "/opt/target"},
            }
        ]
    }

    _normalise_optional_fields(payload)

    assert payload["tool_requests"] == [
        {
            "tool_call_id": "model-tool-1",
            "tool_name": "host_list",
            "arguments": {"service_name": "api-gateway", "path": "/opt/target"},
        }
    ]


def test_normalise_optional_fields_converts_name_input_without_type() -> None:
    payload = {
        "tool_requests": [
            {
                "tool_call_id": "model-tool-2",
                "name": "log_query",
                "input": {"service_name": "api-gateway", "tail_lines": 100},
            }
        ]
    }

    _normalise_optional_fields(payload)

    assert payload["tool_requests"] == [
        {
            "tool_call_id": "model-tool-2",
            "tool_name": "log_query",
            "arguments": {"service_name": "api-gateway", "tail_lines": 100},
        }
    ]


def test_normalise_optional_fields_rejects_non_object_result():
    try:
        _normalise_optional_fields([])
    except ValueError as exc:
        assert "must be an object" in str(exc)
    else:
        raise AssertionError("non-object provider result must be rejected")
