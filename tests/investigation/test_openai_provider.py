"""OpenAI-compatible Provider 的纯本地适配测试。"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from incidentlens_control_plane.investigation.model_transport import (
    ModelTransportError,
    OpenAICompatibleConfig,
)
from incidentlens_control_plane.investigation.openai_provider import (
    OpenAICompatibleProvider,
    _message_payload,
    _normalise_optional_fields,
    _remove_unknown_top_level_tool_arguments,
    _result_payload_from_content,
    _result_payload_from_message,
    _system_prompt,
)
from incidentlens_control_plane.investigation.provider import (
    PromptTooLongError,
    ProviderError,
    ProviderOutputFormatError,
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


class _FakeTransport:
    """Records payloads and returns a canned response or raises a canned error."""

    def __init__(
        self,
        *,
        response: dict[str, object] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    def chat_completions(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


_EMPTY_TURN = json.dumps(
    {
        "tool_requests": [],
        "hypotheses": [],
        "conclusions": [],
        "child_delegation": None,
        "stop_signal": None,
        "usage": {},
    }
)


def _dummy_request() -> SimpleNamespace:
    """A minimal request-shaped object for exercising ``generate_turn``."""
    return SimpleNamespace(
        task_prompt=None,
        checkpoint=SimpleNamespace(
            round_number=1,
            model_dump=lambda mode="json": {"round_number": 1},
        ),
        investigation=SimpleNamespace(
            model_dump=lambda mode="json": {"symptom": "canary 502"},
        ),
        messages=(),
        tool_schemas=(),
    )


def _transcript(role: MessageRole, blocks: tuple) -> TranscriptMessage:
    return TranscriptMessage(
        agent_run_id="run-1",
        sequence=1,
        role=role,
        blocks=blocks,
        created_at=NOW,
    )


async def test_provider_delegates_payload_to_injected_transport(
    provider_config,
) -> None:
    transport = _FakeTransport(
        response={"choices": [{"message": {"content": _EMPTY_TURN}}]}
    )
    provider = OpenAICompatibleProvider(provider_config, transport=transport)

    result = await provider.generate_turn(_dummy_request())

    assert len(transport.calls) == 1
    payload = transport.calls[0]
    assert payload["model"] == "spark-x"
    assert payload["temperature"] == 0.0
    assert payload["response_format"] == {"type": "json_object"}
    assert result.tool_requests == ()


async def test_provider_classifies_schema_invalid_json_as_correctable_format_error(
    provider_config,
) -> None:
    invalid = json.dumps(
        {
            "tool_requests": [],
            "hypotheses": [],
            "conclusions": [{"conclusion": "wrong field"}],
            "child_delegation": None,
            "stop_signal": None,
            "usage": {},
        }
    )
    provider = OpenAICompatibleProvider(
        provider_config,
        transport=_FakeTransport(
            response={"choices": [{"message": {"content": invalid}}]}
        ),
    )

    with pytest.raises(ProviderOutputFormatError, match="summary"):
        await provider.generate_turn(_dummy_request())


async def test_provider_propagates_prompt_too_long_from_transport(
    provider_config,
) -> None:
    transport = _FakeTransport(error=PromptTooLongError())
    provider = OpenAICompatibleProvider(provider_config, transport=transport)
    with pytest.raises(PromptTooLongError):
        await provider.generate_turn(_dummy_request())


async def test_provider_translates_retryable_transport_error_to_provider_error(
    provider_config,
) -> None:
    error = ModelTransportError(
        "OpenAI-compatible API 请求失败（HTTP 429）",
        retryable=True,
        category="http_error",
    )
    provider = OpenAICompatibleProvider(
        provider_config, transport=_FakeTransport(error=error)
    )
    with pytest.raises(ProviderError) as excinfo:
        await provider.generate_turn(_dummy_request())
    assert excinfo.value.retryable is True
    assert "429" in str(excinfo.value)


async def test_provider_translates_non_retryable_transport_error_to_provider_error(
    provider_config,
) -> None:
    error = ModelTransportError(
        "OpenAI-compatible API TLS 证书校验失败",
        retryable=False,
        category="tls_configuration",
    )
    provider = OpenAICompatibleProvider(
        provider_config, transport=_FakeTransport(error=error)
    )
    with pytest.raises(ProviderError) as excinfo:
        await provider.generate_turn(_dummy_request())
    assert excinfo.value.retryable is False


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


def test_removes_only_unknown_top_level_arguments_for_strict_tool_schema() -> None:
    payload = {
        "tool_requests": [
            {
                "tool_call_id": "call-1",
                "tool_name": "host_list",
                "arguments": {
                    "service_name": "api-gateway",
                    "path": "/opt/app",
                    "limit": 100,
                },
            }
        ]
    }
    schema = SimpleNamespace(
        tool_name="host_list",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "path": {"type": "string"},
            },
            "additionalProperties": False,
        },
    )

    _remove_unknown_top_level_tool_arguments(payload, (schema,))

    assert payload["tool_requests"][0]["arguments"] == {
        "service_name": "api-gateway",
        "path": "/opt/app",
    }


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


def test_normalise_host_child_scope_discards_incompatible_identity_fields() -> None:
    payload = {
        "tool_requests": [
            {
                "tool_call_id": "call-1",
                "tool_name": "delegate_child",
                "arguments": {
                    "scope": {
                        "project_id": "project-1",
                        "target_id": "target-1",
                        "scope": "host",
                        "service_name": "api-gateway",
                        "container_name": "api-gateway-1",
                        "allowed_host_paths": ["/srv/payment"],
                    }
                },
            }
        ]
    }

    _normalise_optional_fields(payload)

    scope = payload["tool_requests"][0]["arguments"]["scope"]
    assert scope == {
        "project_id": "project-1",
        "target_id": "target-1",
        "scope": "host",
        "allowed_host_paths": ["/srv/payment"],
    }


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


def test_normalise_optional_fields_discards_citation_only_hypothesis() -> None:
    """An evidence pointer without a claim is not a hypothesis proposal."""
    payload = {
        "tool_requests": [],
        "hypotheses": [{"evidence_ids": ["ev-1"]}],
        "conclusions": [],
        "child_delegation": None,
        "stop_signal": None,
    }

    _normalise_optional_fields(payload)

    assert payload["hypotheses"] == []


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


def _request_at(round_number: int) -> SimpleNamespace:
    """A minimal parent turn-shaped request observed at *round_number*."""
    request = _dummy_request()
    request.checkpoint.round_number = round_number
    return request


@pytest.mark.parametrize("round_number", [1, 3, 8, 12, 50])
def test_parent_prompt_does_not_encode_round_workflow(round_number: int) -> None:
    prompt = _system_prompt(_request_at(round_number))
    assert "本轮为" not in prompt
    assert "只能调用 file_edit" not in prompt
    assert "必须只调用 compact_context" not in prompt
    assert "受保护路径" in prompt


async def _provider_payload(
    provider_config: OpenAICompatibleConfig, tool_names: tuple[str, ...]
) -> dict:
    """One recorded ``generate_turn`` payload for the given registered tools."""
    transport = _FakeTransport(
        response={"choices": [{"message": {"content": _EMPTY_TURN}}]}
    )
    provider = OpenAICompatibleProvider(provider_config, transport=transport)
    request = _request_at(1)
    request.tool_schemas = tuple(
        SimpleNamespace(
            tool_name=name,
            description="registered test tool",
            parameters_json_schema={"type": "object", "additionalProperties": False},
        )
        for name in tool_names
    )
    await provider.generate_turn(request)
    return transport.calls[0]


async def test_prompt_exposes_actual_tools_not_scripted_stage(provider_config) -> None:
    payload = await _provider_payload(provider_config, ("host_read", "file_edit"))
    assert [tool["function"]["name"] for tool in payload["tools"]] == [
        "host_read",
        "file_edit",
    ]
