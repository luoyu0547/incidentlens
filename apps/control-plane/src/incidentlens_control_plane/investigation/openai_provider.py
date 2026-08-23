"""Provider adapter for OpenAI-compatible chat-completions APIs."""

from __future__ import annotations

import asyncio
import html
import json
import re

from incidentlens_control_plane.investigation.model_transport import (
    ModelTransportError,
    OpenAICompatibleConfig,
    OpenAICompatibleTransport,
)
from incidentlens_control_plane.investigation.provider import (
    AgentTurnResult,
    ConversationRequest,
    ModelProvider,
    ProviderError,
    ToolSchema,
)
from incidentlens_control_plane.investigation.types import (
    MessageRole,
    ProviderUsage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
)


class OpenAICompatibleProvider(ModelProvider):
    """Send bounded turns to a provider without granting it execution access."""

    def __init__(
        self, config: OpenAICompatibleConfig, transport: OpenAICompatibleTransport
    ) -> None:
        self._config = config
        self._transport = transport

    async def generate_turn(self, request: ConversationRequest) -> AgentTurnResult:
        payload = {
            "model": self._config.model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _system_prompt(request)},
                *_context_attachments(request),
                *(_message_payload(message) for message in request.messages),
            ],
            "tools": [_tool_payload(schema) for schema in request.tool_schemas],
        }
        try:
            response = await asyncio.to_thread(
                self._transport.chat_completions, payload
            )
        except ModelTransportError as exc:
            raise ProviderError(exc.message, retryable=exc.retryable) from exc
        try:
            message = response["choices"][0]["message"]
            result_payload = _result_payload_from_message(message)
            _normalise_optional_fields(result_payload)
            _remove_unknown_top_level_tool_arguments(
                result_payload, request.tool_schemas
            )
            usage = response.get("usage", {})
            result_payload["usage"] = ProviderUsage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
                output_bytes=len(str(message.get("content") or "").encode("utf-8")),
            ).model_dump()
            return AgentTurnResult.model_validate(result_payload)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            # Keep the diagnostic bounded and free of request/response bodies:
            # callers need the schema reason to distinguish a provider-format
            # issue from a transport error, but raw model text may be sensitive.
            raise ProviderError(
                f"OpenAI-compatible API 返回的结构化调查回合无效：{str(exc)[:500]}",
                retryable=True,
            ) from exc

def _message_payload(message: TranscriptMessage) -> dict[str, object]:
    if message.role is MessageRole.ASSISTANT:
        content = _assistant_content(message.blocks)
        return {
            "role": "assistant",
            "content": json.dumps(content, ensure_ascii=False),
        }
    content = _user_content(message.blocks)
    return {"role": "user", "content": json.dumps(content, ensure_ascii=False)}


def _assistant_content(blocks: tuple) -> list[dict[str, object]]:
    content: list[dict[str, object]] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            content.append(
                {
                    "type": "tool_use",
                    "id": block.tool_call_id,
                    "name": block.tool_name,
                    "input": block.arguments,
                }
            )
    return content


def _user_content(blocks: tuple) -> list[dict[str, object]]:
    content: list[dict[str, object]] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolResultBlock):
            item: dict[str, object] = {
                "type": "tool_result",
                "tool_call_id": block.tool_call_id,
                "content": block.content,
            }
            if block.evidence_ids:
                item["evidence_ids"] = block.evidence_ids
            content.append(item)
    return content


def _context_attachments(request: ConversationRequest) -> tuple[dict[str, str], ...]:
    """Render the bounded checkpoint/investigation context as a leading message.

    The transcript carries the back-and-forth; the checkpoint and investigation
    snapshot still need to be visible every turn so the model can stay in
    bounds.  The child ``task_prompt`` (when present) rides along.
    """
    context: dict[str, object] = {
        "checkpoint": request.checkpoint.model_dump(mode="json"),
        "investigation": request.investigation.model_dump(mode="json"),
    }
    if request.task_prompt is not None:
        context["task_prompt"] = request.task_prompt
    return (
        {
            "role": "user",
            "content": json.dumps(context, ensure_ascii=False),
        },
    )


def _system_prompt(request: ConversationRequest) -> str:
    """Return stable, model-directed agent guidance — never a round schedule.

    The provider advertises the *actual* registered tools in ``request.tool_schemas``
    and the bounded scope/evidence snapshot in the context attachment; the model
    decides how to investigate, delegate, compact, repair, verify and stop.  The
    child ``task_prompt`` (when present) rides along in the context attachment
    and tells a child to finish its own narrow task instead of re-solving the
    whole incident.
    """
    return _SYSTEM_PROMPT


def _tool_payload(schema: ToolSchema) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": schema.tool_name,
            "description": schema.description,
            "parameters": schema.parameters_json_schema,
        },
    }


def _strip_fence(content: object) -> str:
    if not isinstance(content, str):
        raise ValueError("message.content must be a string")
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        return text.split("\n", 1)[1].rsplit("\n", 1)[0]
    return text


_DSML_INVOKE_RE = re.compile(
    r'<｜｜DSML｜｜invoke\s+name="(?P<name>[^"]+)">(?P<body>.*?)</｜｜DSML｜｜invoke>',
    re.DOTALL,
)
_DSML_PARAMETER_RE = re.compile(
    r'<｜｜DSML｜｜parameter\s+(?P<attrs>[^>]*)>(?P<value>.*?)</｜｜DSML｜｜parameter>',
    re.DOTALL,
)
_DSML_NAME_RE = re.compile(r'name="(?P<name>[^"]+)"')
_HYPOTHESIS_PROPOSAL_FIELDS = frozenset(
    {"summary", "facts", "inferences", "unknowns", "evidence_ids"}
)


def _result_payload_from_content(content: object) -> dict[str, object]:
    """Decode JSON output plus DeepSeek's appended DSML tool invocation.

    DeepSeek v4-flash may return a valid JSON result followed by a DSML tool
    block when Chat Completions receives both ``response_format`` and tools.
    DSML is not executable: this function only converts the declared name and
    arguments into the existing proposal shape, which is validated downstream.
    """
    text = _strip_fence(content)
    dsml_only = _deepseek_dsml_tool_requests(text)
    if dsml_only is not None:
        return {
            "tool_requests": dsml_only,
            "hypotheses": [],
            "conclusions": [],
            "child_delegation": None,
            "stop_signal": None,
            "usage": {},
        }
    decoder = json.JSONDecoder()
    payload, end = decoder.raw_decode(text)
    # Some otherwise OpenAI-compatible providers wrap a single JSON result in
    # an array when tools are present.  It remains one declarative provider
    # turn, so unwrap only that precise harmless shape; multiple items are
    # never merged or interpreted as extra operations.
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        payload = payload[0]
    # v4-flash can also serialize several declarative ``tool_use`` objects as
    # the top-level JSON array.  Treat that narrow, all-tool shape like the
    # normal ``tool_requests`` field; do not merge arbitrary result objects.
    if isinstance(payload, list) and payload:
        tool_items = [
            item
            for item in payload
            if isinstance(item, dict)
            and (
                item.get("type") == "tool_use"
                or (
                    isinstance(item.get("name"), str)
                    and any(key in item for key in ("input", "arguments", "parameters"))
                )
            )
        ]
        text_items = [
            item
            for item in payload
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if tool_items and len(tool_items) + len(text_items) == len(payload):
            payload = {
                "tool_requests": tool_items,
                "hypotheses": [],
                "conclusions": [],
                "child_delegation": None,
                "stop_signal": None,
                "usage": {},
            }
    if not isinstance(payload, dict):
        if isinstance(payload, list):
            item_shapes = [
                (
                    sorted(str(key) for key in item)[:6]
                    if isinstance(item, dict)
                    else type(item).__name__
                )
                for item in payload[:4]
            ]
            raise ValueError(
                "provider result must be an object "
                f"(top-level list length={len(payload)}, item_shapes={item_shapes!r})"
            )
        raise ValueError(f"provider result must be an object (got {type(payload).__name__})")
    suffix = text[end:].strip()
    if not suffix:
        return payload
    tool_requests = _deepseek_dsml_tool_requests(suffix)
    if tool_requests is None:
        raise ValueError("unexpected content after provider JSON result")
    if payload.get("tool_requests"):
        raise ValueError("provider returned both JSON and DSML tool requests")
    payload["tool_requests"] = tool_requests
    return payload


def _result_payload_from_message(message: object) -> dict[str, object]:
    """Decode content or standard OpenAI function-call fields from one message."""
    if not isinstance(message, dict):
        raise ValueError("choice.message must be an object")
    standard_calls = message.get("tool_calls")
    content = message.get("content")
    if isinstance(standard_calls, list) and standard_calls:
        if isinstance(content, str) and content.strip():
            payload = _result_payload_from_content(content)
            if payload.get("tool_requests"):
                raise ValueError("provider returned content and standard tool requests")
        elif content in (None, ""):
            payload = {
                "tool_requests": [],
                "hypotheses": [],
                "conclusions": [],
                "child_delegation": None,
                "stop_signal": None,
                "usage": {},
            }
        else:
            raise ValueError("message.content must be text or null")
        payload["tool_requests"] = [_standard_tool_request(item) for item in standard_calls]
        return payload
    return _result_payload_from_content(content)


def _standard_tool_request(item: object) -> dict[str, object]:
    """Convert one OpenAI function call into a proposal, never an execution."""
    if not isinstance(item, dict):
        raise ValueError("standard tool call must be an object")
    function = item.get("function")
    if not isinstance(function, dict):
        raise ValueError("standard tool call is missing function")
    name = function.get("name")
    raw_arguments = function.get("arguments")
    if not isinstance(name, str) or not isinstance(raw_arguments, str):
        raise ValueError("standard tool call must have name and JSON arguments")
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError("standard tool arguments are invalid JSON") from exc
    if not isinstance(arguments, dict):
        raise ValueError("standard tool arguments must be an object")
    tool_call_id = item.get("id")
    if not isinstance(tool_call_id, str):
        raise ValueError("standard tool call is missing id")
    return {
        "tool_call_id": tool_call_id,
        "tool_name": name,
        "arguments": arguments,
    }


def _deepseek_dsml_tool_requests(suffix: str) -> list[dict[str, object]] | None:
    """Parse DeepSeek's narrow DSML tool block, or reject unrelated suffixes."""
    if not (
        suffix.startswith("<｜｜DSML｜｜tool_calls>")
        and suffix.endswith("</｜｜DSML｜｜tool_calls>")
    ):
        return None
    requests: list[dict[str, object]] = []
    for index, invoke in enumerate(_DSML_INVOKE_RE.finditer(suffix), start=1):
        arguments: dict[str, object] = {}
        for parameter in _DSML_PARAMETER_RE.finditer(invoke.group("body")):
            name_match = _DSML_NAME_RE.search(parameter.group("attrs"))
            if name_match is None:
                raise ValueError("DSML parameter is missing its name")
            arguments[name_match.group("name")] = html.unescape(
                parameter.group("value").strip()
            )
        requests.append(
            {
                "tool_call_id": f"deepseek-dsml-{index}",
                "tool_name": invoke.group("name"),
                "arguments": arguments,
            }
        )
    if not requests:
        raise ValueError("DSML tool block has no invocations")
    return requests


def _remove_unknown_top_level_tool_arguments(
    payload: object, schemas: tuple[ToolSchema, ...]
) -> None:
    """Drop provider-added top-level keys before strict runtime validation.

    This applies only when a tool schema explicitly forbids additional
    properties. Required fields, nested values, scope, policy and approvals
    remain unchanged and are still checked by ``ProviderOutputValidator``.
    """
    if not isinstance(payload, dict):
        return
    requests = payload.get("tool_requests")
    if not isinstance(requests, list):
        return
    by_name = {schema.tool_name: schema.parameters_json_schema for schema in schemas}
    for request in requests:
        if not isinstance(request, dict):
            continue
        schema = by_name.get(request.get("tool_name"))
        arguments = request.get("arguments")
        if (
            not isinstance(schema, dict)
            or schema.get("additionalProperties") is not False
            or not isinstance(schema.get("properties"), dict)
            or not isinstance(arguments, dict)
        ):
            continue
        allowed = schema["properties"].keys()
        request["arguments"] = {
            key: value for key, value in arguments.items() if key in allowed
        }


def _normalise_optional_fields(payload: object) -> None:
    """只规范 model 常见的空值表示，不补造任何操作或外部事实。"""
    if not isinstance(payload, dict):
        raise ValueError("provider result must be an object")
    # DeepSeek may emit one OpenAI-style tool-use object as the whole content
    # instead of putting it under ``tool_requests``.  Wrap exactly that one
    # declarative proposal; the ordinary request validator still owns tool
    # allowlisting, arguments, scope, policy, approval and execution.
    if (
        "tool_requests" not in payload
        and (
            payload.get("type") == "tool_use"
            or (
                isinstance(payload.get("name"), str)
                and any(key in payload for key in ("input", "arguments", "parameters"))
            )
        )
    ):
        tool_request = dict(payload)
        payload.clear()
        payload.update(
            {
                "tool_requests": [tool_request],
                "hypotheses": [],
                "conclusions": [],
                "child_delegation": None,
                "stop_signal": None,
            }
        )
    if payload.get("child_delegation") == []:
        payload["child_delegation"] = None
    tool_requests = payload.get("tool_requests")
    if isinstance(tool_requests, list):
        for tool_request in tool_requests:
            if not isinstance(tool_request, dict):
                continue
            if "arguments" not in tool_request and isinstance(
                tool_request.get("parameters"), dict
            ):
                # OpenAI-style tools are commonly documented with
                # ``parameters``; internally our proposal contract calls this
                # payload ``arguments``.  This is a name-only conversion: the
                # downstream schema/scope validator still validates every key.
                tool_request["arguments"] = tool_request.pop("parameters")
            if "tool_call_id" not in tool_request and isinstance(tool_request.get("id"), str):
                tool_request["tool_call_id"] = tool_request.pop("id")
            if "tool_name" not in tool_request and isinstance(tool_request.get("name"), str):
                tool_request["tool_name"] = tool_request.pop("name")
            if "arguments" not in tool_request and isinstance(tool_request.get("input"), dict):
                tool_request["arguments"] = tool_request.pop("input")
            if tool_request.get("type") == "tool_use":
                tool_request.pop("type")
            arguments = tool_request.get("arguments")
            if tool_request.get("tool_name") == "delegate_child" and isinstance(
                arguments, dict
            ):
                child_scope = arguments.get("scope")
                if isinstance(child_scope, dict) and child_scope.get("scope") == "host":
                    # These identities are invalid for a host-scoped AgentScope.
                    # Some compatible models copy them from the parent service;
                    # dropping only the schema-incompatible optional keys keeps
                    # the requested target and path bounds unchanged.
                    child_scope.pop("service_name", None)
                    child_scope.pop("container_name", None)
            if tool_request.get("tool_name") == "todo_write" and isinstance(arguments, dict):
                todos = arguments.get("todos")
                if isinstance(todos, str):
                    try:
                        decoded_todos = json.loads(todos)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(decoded_todos, list):
                        arguments["todos"] = decoded_todos
    hypotheses = payload.get("hypotheses")
    if isinstance(hypotheses, list):
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, dict):
                continue
            if "summary" not in hypothesis:
                description = hypothesis.get("description")
                title = hypothesis.get("title")
                if isinstance(description, str):
                    hypothesis["summary"] = description
                elif isinstance(title, str):
                    hypothesis["summary"] = title
            hypothesis.pop("description", None)
            # Some OpenAI-compatible models add a display-only ``title``.
            # The internal proposal contract owns a single summary field.
            hypothesis.pop("title", None)
            # Hypothesis identifiers are assigned by the runtime after the
            # proposal is accepted; accepting a model-supplied id would break
            # that ownership boundary.
            hypothesis.pop("hypothesis_id", None)
            # Provider-facing labels such as ``status`` and ``confidence`` do
            # not affect an operation. Keep only the strict proposal contract
            # before Pydantic validates it.
            for key in set(hypothesis) - _HYPOTHESIS_PROPOSAL_FIELDS:
                hypothesis.pop(key)
    stop_signal = payload.get("stop_signal")
    if isinstance(stop_signal, dict) and "summary" not in stop_signal:
        reason = stop_signal.get("stop_reason", "unknown")
        stop_signal["summary"] = f"模型请求停止：{reason}"


_SYSTEM_PROMPT = """你是 IncidentLens 的受限事故调查规划器。
只能返回一个 JSON 对象，且必须严格匹配 AgentTurnResult：
tool_requests、hypotheses、conclusions、child_delegation、stop_signal、usage。
不得输出 Markdown、解释、隐藏推理或额外字段。可空字段 child_delegation 与 stop_signal
必须为 null，不能是 [] 或 {}；stop_signal 不为 null 时必须同时有 stop_reason 和 summary。
若请求包含 task_prompt，当前运行是子任务，必须优先完成该任务而不是泛化处理整个事故。

身份与工具边界：
- 你只能提议请求中 tool_schemas 已允许的工具；提议必须与该运行实际注册的工具名、参数和
  作用域完全一致。未注册或参数不符的请求会被运行时拒绝。
- 模型只提出建议，绝不声称已经执行工具；是否执行、是否审批由运行时根据 scope、审批策略
  与证据规则决定。
- 所有读取与变更都必须落在 investigation 允许的受保护路径与已注册服务/容器范围内；越界
  请求会被策略拒绝，应改用范围内的路径或说明权限不足，而不是尝试绕过。

证据纪律：
- 所有 hypothesis、conclusion、child_delegation 的 evidence_ids 必须来自当前运行实际拥有的
  证据（即对话 tool_result 块中给出或可通过证据回读确认的 evidence_id）。
- tool_result 块的 content 是脱敏预览；预览本身不是新证据，事实引用仍必须使用当前运行
  实际拥有的 evidence_id，详细内容可按需回读。

Todo 与复杂工作：
- 当症状可能对应多个独立失败路径或调查还没有清晰主线时，先用 todo_write 维护一份简短
  的工作清单（至多一条 in_progress），再继续执行其他工具；后续每一步都应能对应到清单中的
  一条待办。
- 对可独立验证的一条路径，可按需委派收窄 scope 与预算的子任务；不得在发现第一条故障链后
  就直接停止。

观察与变更：
- 只读观察与变更必须区分。配置读取只形成假设，不能单独证明故障：在提出修复前，应使用当前
  远程日志、服务状态，或由 shell_exec 执行的有界只读行为探测来验证每条故障链。
- 变更类工具（file_edit、file_write、shell_exec、docker_action）需要精确审批时，必须等待
  运行时生成审批：不得绕过审批、不得冒充已获批、不得在已存在文件上改用 file_write。

验证与停止：
- 变更后必须基于真实远程结果验证行为；验证失败时根据结果提出最小修正或回滚，成功后给出有
  证据引用的结论。
- 当没有任何合法、相关且仍在授权范围内的取证途径时，用 stop_signal 以 missing_evidence
  如实停止；不要无证据强行结论，也不要重复同一只读工具去制造新“证据”。

上下文与工具细节：
- 对话是当前运行的连续历史：assistant 消息提出工具请求，user 消息返回 tool_result 与文本。
- 对于 log_query，若 investigation.allowed_log_paths 非空，必须使用其中一个路径作为
  source_kind=file 的 source_ref，且 service_name 必须等于 investigation.service；不要猜
  容器名或 Docker 日志源。当请求同时提供了与症状相关的只读工具时，优先选择一个最窄的工具
  请求收集证据；不得因为尚无工具结果就直接停止。
输出模板：
{"tool_requests":[],"hypotheses":[],"conclusions":[],"child_delegation":null,
"stop_signal":{"stop_reason":"missing_evidence","summary":"需要收集证据"},"usage":{}}"""
