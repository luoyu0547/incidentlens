"""Provider adapter for OpenAI-compatible chat-completions APIs."""

from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from incidentlens_control_plane.investigation.provider import (
    AgentTurnResult,
    ConversationRequest,
    ModelProvider,
    PromptTooLongError,
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


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    """Connection configuration for an OpenAI-compatible endpoint."""

    api_key: str
    base_url: str
    model: str
    timeout_seconds: float = 90.0


class OpenAICompatibleProvider(ModelProvider):
    """Send bounded turns to a provider without granting it execution access."""

    def __init__(self, config: OpenAICompatibleConfig) -> None:
        self._config = config

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
        response = await asyncio.to_thread(self._post, payload)
        try:
            message = response["choices"][0]["message"]
            result_payload = _result_payload_from_message(message)
            _normalise_optional_fields(result_payload)
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
                body = json.loads(response.read().decode("utf-8"))
                if _context_length_exceeded(body):
                    raise PromptTooLongError()
                return body
        except HTTPError as exc:
            if exc.code == 413:
                raise PromptTooLongError() from exc
            retryable = exc.code in {408, 429, 500, 502, 503, 504}
            raise ProviderError(
                f"OpenAI-compatible API 请求失败（HTTP {exc.code}）",
                retryable=retryable,
            ) from exc
        except (TimeoutError, URLError) as exc:
            raise ProviderError("OpenAI-compatible API 连接失败", retryable=True) from exc


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
    """Add a bounded, capability-only cadence for parallel investigations.

    The base prompt defines *when* parallel work is warranted.  This appendix
    makes the first four parent turns operationally unambiguous without naming
    any service, fault, file, command or repair: establish alternatives, take
    one observation, delegate one bounded branch, then compact before acting.
    It prevents a weaker compatible model from endlessly re-reading the same
    source while preserving its freedom to choose all actual observations and
    conclusions.
    """
    if request.task_prompt is not None:
        return _SYSTEM_PROMPT
    round_number = request.checkpoint.round_number
    has_child_delegation = any(
        isinstance(block, ToolUseBlock) and block.tool_name == "delegate_child"
        for message in request.messages
        for block in message.blocks
    )
    has_compaction = any(
        isinstance(block, ToolUseBlock) and block.tool_name == "compact_context"
        for message in request.messages
        for block in message.blocks
    )
    if round_number == 1:
        return _SYSTEM_PROMPT + """
本轮为并行症状调查的建模阶段：必须同时提出至少两个可检验 hypothesis，并使用 todo_write
保存至少两条独立路径；可附带一项最窄的只读观察。不得停止或提出变更。"""
    if round_number == 2:
        return _SYSTEM_PROMPT + """
本轮为初始取证阶段：必须提出一项最窄的远程只读 Observation，为某一条已保存路径收集证据。
不得提出远程变更或停止。"""
    if round_number == 3 and not has_child_delegation:
        return _SYSTEM_PROMPT + """
本轮为并行阶段：必须调用 delegate_child，把另一条独立路径交给收窄的已注册 scope 与小预算。
不得继续重复父任务 Observation、提出变更或停止。"""
    # A rejected first delegation must not make us skip the memory boundary:
    # after a later retry succeeds, require compaction before any further
    # investigation turn can drift into repeated reads or a proposed change.
    # Some compatible providers do not retain a tool-use block in the next
    # bounded transcript.  Round five is the first turn after the required
    # delegation retry window, so it supplies a stable backstop for the same
    # boundary without teaching the model anything about a particular target.
    if (has_child_delegation or round_number >= 5) and not has_compaction:
        return _SYSTEM_PROMPT + """
本轮为上下文边界阶段：必须只调用 compact_context。压缩完成后，任何需要的当前细节都要通过
新的远程 Observation 重新获取；不得依据旧 tool_result 预览直接提出变更。"""
    if not has_child_delegation:
        return _SYSTEM_PROMPT + """
并行阶段尚未完成：现在必须只调用 delegate_child，把一条独立路径委派给收窄的已注册 scope
与小预算。不能继续父任务 Observation、提出变更或停止。"""
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


def _context_length_exceeded(payload: object) -> bool:
    """Return True when an OpenAI-style error body names a context overflow."""
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    if isinstance(code, str) and "context_length_exceeded" in code:
        return True
    message = error.get("message")
    return isinstance(message, str) and "context_length_exceeded" in message.lower()


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
不得输出 Markdown、解释、隐藏推理或额外字段。你只能提议请求中 tool_schemas 已允许的工具；
所有 hypothesis/conclusion/child_delegation 的 evidence_ids 必须来自当前运行实际拥有的证据
（即对话 tool_result 块中给出或可通过证据回读确认的 evidence_id）。
可空字段 child_delegation 与 stop_signal 必须为 null，不能是 [] 或 {}。
stop_signal 不为 null 时必须同时有 stop_reason 和 summary。
模型只提出建议，绝不声称已经执行工具。
若请求包含 task_prompt，当前运行是子任务，必须优先完成该任务而不是泛化处理整个事故。
对话是当前运行的连续历史：assistant 消息提出工具请求，user 消息返回 tool_result 与文本。
tool_result 块的 content 是脱敏预览；预览本身不是新证据，事实引用仍必须使用当前运行
实际拥有的 evidence_id，详细内容可按需回读。
当症状暗示多个独立失败路径时，先维护 Todo，并为可独立验证的一条路径委派收窄 scope
和预算的子任务；不得在发现第一条故障链后停止。配置读取只形成假设，不能单独证明故障：
在提出修复前，必须用当前远程日志、服务状态，或由 shell_exec 执行的有界只读行为探测来
验证每条故障链。双路径调查在保存 Todo、得到初步观察后、提出任何远程变更前，必须调用
一次 compact_context；压缩后需要当前细节时应重新调用远程 Observation 工具，而不是以旧
Evidence 预览替代仍可重取的状态。
首次回合且请求提供了与症状相关的
只读工具时，优先从 tool_schemas 中选择一个最窄的工具请求来收集证据：例如
已给出授权日志文件时可提出一次 log_query；不得因为尚无工具结果就直接停止。
对于 log_query，若 investigation.allowed_log_paths 非空，必须使用其中一个路径作为
source_kind=file 的 source_ref，且 service_name 必须等于 investigation.service；不要猜测
容器名或 Docker 日志源。
当已有 tool_result 能直接解释 symptom 时，不要重复调用同一工具：提出
一条仅引用这些 evidence_ids 的 conclusion，并设置 stop_signal 为 completed。结论的
summary、facts、evidence_ids 必须使用 AgentTurnResult 的字段名。
只有没有任何合法、相关的只读取证工具时，才使用 stop_signal，stop_reason 为
missing_evidence。每次最多提出一个工具请求，并为 tool_call_id 使用简短唯一标识。
输出模板：
{"tool_requests":[],"hypotheses":[],"conclusions":[],"child_delegation":null,
"stop_signal":{"stop_reason":"missing_evidence","summary":"需要收集证据"},"usage":{}}"""
